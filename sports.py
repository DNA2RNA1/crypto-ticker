"""Live scores, recent finals and upcoming games from ESPN's public JSON.

Leagues are defined in one table (LEAGUES); adding one is a single line. For each
enabled, in-season league the ticker follows:
  * top teams: the AP Top 25 for college (or top 10), the best records for pro leagues
  * favorites picked on the phone page (by ESPN team id)
A game is shown if either team is followed. Order on the panel: live games, then finals
from the last 24 hours, then the next game for each favorite.

Data refreshes every 60 s while a followed game is live, otherwise every 15 minutes.
Every league keeps its last good data; if ESPN fails or changes its format, that league's
games are dropped (or the screen is skipped) and nothing crashes.
"""

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("ticker.sports")

SITE = "https://site.api.espn.com/apis/site/v2/sports"
STANDINGS = "https://site.api.espn.com/apis/v2/sports"
ET = ZoneInfo("America/New_York")   # ESPN's scoreboard dates are US Eastern days
HERE = os.path.dirname(os.path.abspath(__file__))
TEAMS_CACHE = os.path.join(HERE, "cache", "teams-{}.json")


@dataclass(frozen=True)
class League:
    key: str                 # settings key: SPORTS_<KEY>=on, SPORTS_<KEY>_TOP, SPORTS_<KEY>_FAVORITES
    path: str                # ESPN path after /sports/
    name: str                # shown on the panel and phone page
    rankings: bool           # college: follow the AP poll; pro: follow the best records
    months: tuple            # months the season runs (1-12), incl. playoffs / bowls
    default_on: bool = True
    default_top: int = 8     # pro: top N by record; college: top N of the AP poll
    color: tuple = (120, 120, 120)                     # tag color on the panel
    params: dict = field(default_factory=dict, hash=False, compare=False)  # extra scoreboard params


#   key       ESPN path                              name     rank   season months                     on     top  tag color          extra
LEAGUES = [
    League("nfl", "football/nfl", "NFL", False, (9, 10, 11, 12, 1, 2), True, 8, (0, 90, 200)),
    League("ncaaf", "football/college-football", "NCAAF", True, (8, 9, 10, 11, 12, 1), True, 25, (200, 30, 40),
           {"groups": "80", "limit": "300"}),
    League("nba", "basketball/nba", "NBA", False, (10, 11, 12, 1, 2, 3, 4, 5, 6), True, 8, (230, 90, 20)),
    League("ncaam", "basketball/mens-college-basketball", "NCAAM", True, (11, 12, 1, 2, 3, 4), True, 25,
           (30, 120, 220), {"groups": "50", "limit": "400"}),
    League("mlb", "baseball/mlb", "MLB", False, (3, 4, 5, 6, 7, 8, 9, 10, 11), True, 8, (0, 150, 90)),
    League("nhl", "hockey/nhl", "NHL", False, (10, 11, 12, 1, 2, 3, 4, 5, 6), False, 8, (140, 140, 160)),
]
BY_KEY = {lg.key: lg for lg in LEAGUES}
TOP_CHOICES = {True: (0, 10, 25), False: (0, 4, 8, 12, 16)}  # college / pro, 0 = off

LIVE_EVERY = 60          # seconds between fetches while a followed game is live
IDLE_EVERY = 900
RANKINGS_EVERY = 6 * 3600
SCHEDULE_EVERY = 900
TEAMS_EVERY = 7 * 86400
MAX_GAMES = 12
FINAL_WINDOW = 24 * 3600
GAME_LENGTH = 3 * 3600   # roughly when a game that started at "start" went final


def in_season(league, now=None):
    return (now or datetime.now()).month in league.months


# --- parsing ESPN JSON (defensive: anything odd is skipped, never raised) ----------------
def _ts(text):
    if not isinstance(text, str) or not text:
        return None
    text = text.strip().replace("Z", "+00:00")
    if len(text) == 16 + 6 and text[16] in "+-":   # 2026-11-02T18:00+00:00 (no seconds)
        text = text[:16] + ":00" + text[16:]
    try:
        d = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _score(v):
    if isinstance(v, dict):
        v = v.get("value", v.get("displayValue"))
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _rank(c):
    r = (c.get("curatedRank") or {}).get("current") if isinstance(c.get("curatedRank"), dict) else c.get("rank")
    try:
        r = int(r)
    except (TypeError, ValueError):
        return None
    return r if 1 <= r <= 25 else None   # ESPN uses 99 for unranked


def _record(c):
    for rec in c.get("records") or c.get("record") or []:
        if isinstance(rec, dict) and rec.get("type", "total") in ("total", "overall", None):
            return rec.get("summary") or rec.get("displayValue")
    return None


def win_pct(summary):
    """'7-2' -> 0.778; '40-30-5' (NHL W-L-OTL) -> 0.533; None if unknown."""
    try:
        parts = [int(p) for p in str(summary).split("-")]
    except (TypeError, ValueError):
        return None
    games = sum(parts)
    return parts[0] / games if games else None


def parse_team(c):
    team = c.get("team") or {}
    tid, abbr = str(team.get("id") or c.get("id") or ""), team.get("abbreviation")
    if not tid or not isinstance(abbr, str) or not abbr:
        raise ValueError("competitor without team id/abbreviation")
    return {"id": tid, "abbr": abbr.upper()[:6], "name": team.get("displayName") or team.get("name") or abbr,
            "color": team.get("color"), "alt": team.get("alternateColor"),
            "score": _score(c.get("score")), "rank": _rank(c), "record": _record(c),
            "home": c.get("homeAway") == "home", "winner": bool(c.get("winner"))}


# Baseball and hockey are bet on the moneyline (their spreads are almost always 1.5)
MONEYLINE_LEAGUES = ("mlb", "nhl")


def _line(v):
    """A spread or total as the panel shows it: -3.5, +3, 47.5 (None if not a number)."""
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    if f != f or abs(f) > 999:
        return None
    return f"{f:g}"


def _moneyline(side, odds, which):
    """A moneyline like +145 / -170: ``homeTeamOdds.moneyLine`` (int), or the newer
    ``moneyline.home.close.odds`` (string). None if neither is there."""
    v = (odds.get(f"{which}TeamOdds") or {}).get("moneyLine")
    if v is None:
        node = (odds.get("moneyline") or {}).get(which) or {}
        v = ((node.get("close") or node.get("open") or {}).get("odds"))
    if isinstance(v, str):
        v = v.strip().upper()
        if v in ("EVEN", "EV"):
            return "EVEN"
        try:
            v = float(v)
        except ValueError:
            return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) < 100 or abs(v) > 99999:
        return None
    return f"{int(v):+d}"


def parse_odds(comp, away, home):
    """The pre-game betting line from an ESPN competition's ``odds`` list, for display:
    {"spread": {"away": "+3.5", "home": "-3.5"} or None, "total": "47.5" or None,
     "ml": {"away": "+145", "home": "-170"} or None}. None if there's no line at all.

    The spread comes from ``details`` ("KC -3.5", "EVEN"): the named team gets the
    number, the other team the opposite. Odd formats give no spread rather than a wrong one."""
    lines = comp.get("odds") or []
    odds = next((o for o in lines if isinstance(o, dict)), None)
    if not odds:
        return None
    spread = None
    details = str(odds.get("details") or "").strip().upper()
    m = re.fullmatch(r"([A-Z0-9&.]{1,6})\s+([+-]?\d{1,3}(?:\.\d)?)", details)
    if details in ("EVEN", "PK", "PICK", "PICK'EM"):
        spread = {"away": "PK", "home": "PK"}
    elif m and m[1] in (away["abbr"], home["abbr"]):
        n = float(m[2])
        fav, dog = ("home", "away") if m[1] == home["abbr"] else ("away", "home")
        spread = {fav: f"{n:+g}", dog: f"{-n:+g}"} if n else {"away": "PK", "home": "PK"}
    total = _line(odds.get("overUnder"))
    ml = {"away": _moneyline(away, odds, "away"), "home": _moneyline(home, odds, "home")}
    ml = ml if ml["away"] and ml["home"] else None
    if not (spread or total or ml):
        return None
    return {"spread": spread, "total": total, "ml": ml}


def parse_event(ev, league):
    """One ESPN event -> game dict, or None if it doesn't look like a game."""
    try:
        comp = (ev.get("competitions") or [None])[0] or {}
        status = comp.get("status") or ev.get("status") or {}
        stype = status.get("type") or {}
        state = stype.get("state")
        if state not in ("pre", "in", "post"):
            return None
        teams = [parse_team(c) for c in comp.get("competitors") or []]
        if len(teams) != 2:
            return None
        home = next((t for t in teams if t["home"]), teams[1])
        away = next((t for t in teams if t is not home), teams[0])
        start = _ts(ev.get("date") or comp.get("date"))
        if start is None:
            return None
        try:
            odds = parse_odds(comp, away, home) if state == "pre" else None
        except (AttributeError, TypeError, ValueError):
            odds = None
        return {"id": str(ev.get("id") or comp.get("id") or f"{away['id']}@{home['id']}:{start}"),
                "league": league.key, "state": state, "start": start,
                "detail": (stype.get("shortDetail") or stype.get("detail") or stype.get("description") or "").strip(),
                "completed": bool(stype.get("completed")), "away": away, "home": home, "odds": odds}
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        log.debug("skipping odd %s event: %s", league.key, exc)
        return None


def parse_scoreboard(js, league):
    if not isinstance(js, dict) or not isinstance(js.get("events"), list):
        raise ValueError(f"{league.key}: scoreboard JSON has no events list")
    return [g for g in (parse_event(ev, league) for ev in js["events"] if isinstance(ev, dict)) if g]


def parse_rankings(js):
    """AP poll -> {team_id: rank}. Falls back to the first poll listed."""
    polls = js.get("rankings") if isinstance(js, dict) else None
    if not isinstance(polls, list) or not polls:
        raise ValueError("rankings JSON has no polls")
    ap = next((p for p in polls if isinstance(p, dict) and (p.get("type") == "ap" or "AP" in str(p.get("name", "")))),
              polls[0])
    out = {}
    for r in ap.get("ranks") or []:
        try:
            tid, cur = str(r["team"]["id"]), int(r["current"])
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= cur <= 25:
            out[tid] = cur
    if not out:
        raise ValueError("rankings poll is empty")
    return out


def parse_standings(js):
    """Standings (any nesting of conferences/divisions) -> [(win%, wins, team_id)] best first."""
    rows = []

    def walk(node):
        if isinstance(node, dict):
            entries = (node.get("standings") or {}).get("entries") if isinstance(node.get("standings"), dict) else None
            for e in entries or []:
                try:
                    stats = {s.get("name"): s.get("value") for s in e.get("stats") or [] if isinstance(s, dict)}
                    tid = str(e["team"]["id"])
                    wins = float(stats.get("wins") or 0)
                    pct = stats.get("winPercent")
                    if pct is None:
                        losses = float(stats.get("losses") or 0)
                        pct = wins / (wins + losses) if wins + losses else 0.0
                    rows.append((float(pct), wins, tid))
                except (KeyError, TypeError, ValueError):
                    continue
            for child in node.get("children") or []:
                walk(child)

    walk(js)
    if not rows:
        raise ValueError("standings JSON has no teams")
    best = {}
    for row in rows:
        best[row[2]] = max(best.get(row[2], row), row)
    return sorted(best.values(), reverse=True)


def parse_teams(js):
    """Team list -> [{id, abbr, name, color}]."""
    out = []
    try:
        groups = js["sports"][0]["leagues"][0]["teams"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("teams JSON has an unexpected shape") from None
    for item in groups:
        t = (item or {}).get("team") or {}
        if t.get("id") and t.get("abbreviation"):
            out.append({"id": str(t["id"]), "abbr": str(t["abbreviation"]).upper()[:6],
                        "name": t.get("displayName") or t.get("name") or t["abbreviation"],
                        "short": t.get("shortDisplayName") or "", "location": t.get("location") or "",
                        "color": t.get("color")})
    return out


# --- colors ---------------------------------------------------------------------------
def _rgb(hexstr):
    try:
        h = str(hexstr).lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) if len(h) == 6 else None
    except ValueError:
        return None


def team_color(team):
    """The team color, or its alternate if the main one would vanish on a black panel;
    lightened if both are too dark."""
    def lum(c):
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    for c in (_rgb(team.get("color")), _rgb(team.get("alt"))):
        if c and lum(c) >= 55:
            return c
    c = _rgb(team.get("color")) or _rgb(team.get("alt")) or (150, 150, 150)
    m = max(c) or 1
    return tuple(min(255, int(v * 200 / m) + 30) for v in c)   # same hue, bright enough


# --- the feed ---------------------------------------------------------------------------
@dataclass
class Follow:
    on: bool
    top: int
    favorites: list          # [(team_id, abbr)]

    @property
    def favorite_ids(self):
        return {tid for tid, _ in self.favorites}


def parse_favorites(text):
    out = []
    for part in str(text or "").split(","):
        tid, _, abbr = part.strip().partition(":")
        if tid.isdigit() and abbr:
            out.append((tid, abbr.upper()[:6]))
    return out


def follow_from_env(env):
    """{league_key: Follow} from SPORTS_* settings (env is a mapping or os.environ)."""
    out = {}
    for lg in LEAGUES:
        k = lg.key.upper()
        on = str(env.get(f"SPORTS_{k}", "on" if lg.default_on else "off")).strip().lower() in ("on", "true", "1", "yes")
        try:
            top = int(env.get(f"SPORTS_{k}_TOP", lg.default_top))
        except (TypeError, ValueError):
            top = lg.default_top
        top = top if top in TOP_CHOICES[lg.rankings] else lg.default_top
        out[lg.key] = Follow(on, top, parse_favorites(env.get(f"SPORTS_{k}_FAVORITES", "")))
    return out


class Sports:
    """Background fetcher for every enabled, in-season league."""
    _start_lock = threading.Lock()  # the loop and the TV page may both ask at once

    def __init__(self, follow, timeout=15, clock=time.time):
        self.follow = follow
        self.timeout = timeout
        self.clock = clock
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "crypto-ticker/2.0"
        self.boards = {}         # key -> (fetched_at, [games])        last good scoreboard games
        self.top_ids = {}        # key -> (fetched_at, {team_id: rank or order})
        self.schedules = {}      # (key, team_id) -> (fetched_at, [games])
        self.next_fetch = 0.0
        self.last_ok = None
        self.last_error = None
        self._busy = False
        self._lock = threading.Lock()

    # -- which leagues ---------------------------------------------------------------
    def active(self, now=None):
        now_dt = datetime.fromtimestamp(now or self.clock())
        return [lg for lg in LEAGUES if self.follow.get(lg.key, Follow(False, 0, [])).on and in_season(lg, now_dt)]

    # -- background loop -------------------------------------------------------------
    def update(self, now=None):
        """Start a refresh if one is due; return immediately."""
        now = now or self.clock()
        with self._start_lock:
            if now < self.next_fetch or self._busy or not self.active(now):
                return
            self._busy = True
            self.next_fetch = now + IDLE_EVERY
        threading.Thread(target=self._run, daemon=True, name="sports").start()

    def _run(self):
        try:
            self.refresh()
        except Exception as exc:  # never let a format change kill the thread
            log.warning("sports refresh failed: %s", exc)
            self.last_error = str(exc)
        finally:
            self.next_fetch = self.clock() + (LIVE_EVERY if self.live_followed() else IDLE_EVERY)
            self._busy = False

    def _get(self, url, params=None):
        r = self.session.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def refresh(self):
        now = self.clock()
        errors = []
        for lg in self.active(now):
            try:
                self._refresh_league(lg, now)
            except Exception as exc:  # keep this league's last good data
                errors.append(f"{lg.name}: {exc}")
                log.warning("sports %s failed: %s", lg.name, exc)
        self.last_error = "; ".join(errors) or None
        if not errors:
            self.last_ok = now

    def _refresh_league(self, lg, now):
        today = datetime.fromtimestamp(now, ET)
        games = {}
        for day in (today - timedelta(days=1), today):    # yesterday for finals, today for live
            js = self._get(f"{SITE}/{lg.path}/scoreboard", dict(lg.params, dates=day.strftime("%Y%m%d")))
            for g in parse_scoreboard(js, lg):
                games[g["id"]] = g
        with self._lock:
            self.boards[lg.key] = (now, list(games.values()))
        fol = self.follow[lg.key]
        if fol.top and now - self.top_ids.get(lg.key, (0, None))[0] > RANKINGS_EVERY:
            try:
                if lg.rankings:
                    ranks = parse_rankings(self._get(f"{SITE}/{lg.path}/rankings"))
                else:
                    table = parse_standings(self._get(f"{STANDINGS}/{lg.path}/standings"))
                    ranks = {tid: i + 1 for i, (_, _, tid) in enumerate(table)}
                self.top_ids[lg.key] = (now, ranks)
            except Exception as exc:  # fall back to the ranks/records in the scoreboard
                log.info("%s rankings/standings unavailable: %s", lg.name, exc)
        for tid, _ in fol.favorites:
            if any(tid in (g["home"]["id"], g["away"]["id"]) and g["state"] == "pre" for g in games.values()):
                continue  # already playing today
            key = (lg.key, tid)
            if now - self.schedules.get(key, (0, None))[0] > SCHEDULE_EVERY:
                try:
                    js = self._get(f"{SITE}/{lg.path}/teams/{tid}/schedule")
                    sched = parse_scoreboard(js, lg)
                    self.schedules[key] = (now, sched)
                except Exception as exc:
                    log.info("%s schedule for team %s unavailable: %s", lg.name, tid, exc)

    # -- what to show --------------------------------------------------------------------
    def followed(self, lg, games):
        """Team ids followed in ``lg``: favorites plus the current top teams."""
        fol = self.follow.get(lg.key)
        if not fol or not fol.on:
            return set(), set()
        favs = fol.favorite_ids
        top = set()
        if fol.top:
            ranks = self.top_ids.get(lg.key, (0, None))[1]
            if ranks:
                top = {tid for tid, r in ranks.items() if r <= fol.top}
            elif lg.rankings:                 # rankings call failed: use the scoreboard's ranks
                top = {t["id"] for g in games for t in (g["home"], g["away"]) if t["rank"] and t["rank"] <= fol.top}
            else:                             # standings failed: best records seen in the scoreboard
                seen = {}
                for g in games:
                    for t in (g["home"], g["away"]):
                        p = win_pct(t["record"])
                        if p is not None:
                            seen[t["id"]] = p
                top = set(sorted(seen, key=seen.get, reverse=True)[:fol.top])
        return top, favs

    def live_followed(self):
        return any(g["state"] == "in" for g in self.games())

    def games(self, now=None):
        """Games to show, in order: live, finals from the last 24 h, each favorite's next game."""
        now = now or self.clock()
        live, finals, upcoming = [], [], []
        order = {lg.key: i for i, lg in enumerate(LEAGUES)}
        with self._lock:
            boards = dict(self.boards)
        for lg in self.active(now):
            games = (boards.get(lg.key) or (0, []))[1]
            top, favs = self.followed(lg, games)
            followed = top | favs

            def mark(g):
                g = dict(g)
                g["fav"] = {s: g[s]["id"] in favs for s in ("away", "home")}
                g["fetched"] = (boards.get(lg.key) or (0, []))[0]
                return g

            for g in games:
                ids = {g["home"]["id"], g["away"]["id"]}
                if not ids & followed:
                    continue
                if g["state"] == "in":
                    live.append(mark(g))
                elif g["state"] == "post" and now - (g["start"] + GAME_LENGTH) <= FINAL_WINDOW:
                    finals.append(mark(g))
            for tid in sorted(favs):
                pool = [g for g in games if g["state"] == "pre" and tid in (g["home"]["id"], g["away"]["id"])]
                pool += [g for g in (self.schedules.get((lg.key, tid)) or (0, []))[1]
                         if g["state"] == "pre" and g["start"] >= now - 3600]
                if pool:
                    nxt = min(pool, key=lambda g: g["start"])
                    if all(nxt["id"] != u["id"] for u in upcoming):
                        upcoming.append(mark(nxt))
        live.sort(key=lambda g: (order[g["league"]], g["start"]))
        finals.sort(key=lambda g: -g["start"])
        upcoming.sort(key=lambda g: g["start"])
        return (live + finals + upcoming)[:MAX_GAMES]

    def lines(self, now=None, limit=24):
        """Every upcoming game with a betting line on the scoreboards already fetched (the
        leagues you have on), soonest first; your favorites' games are marked. For the TV's
        Betting lines page. No extra requests."""
        now = now or self.clock()
        with self._lock:
            boards = dict(self.boards)
        out = []
        for lg in self.active(now):
            _, favs = self.followed(lg, (boards.get(lg.key) or (0, []))[1])
            for g in (boards.get(lg.key) or (0, []))[1]:
                if g["state"] == "pre" and g.get("odds") and g["start"] >= now - 600:
                    g = dict(g)
                    g["fav"] = {s: g[s]["id"] in favs for s in ("away", "home")}
                    out.append(g)
        out.sort(key=lambda g: (g["start"], g["league"]))
        return out[:limit]

    # -- team search for the phone page -----------------------------------------------------
    def search_teams(self, key, query, limit=12):
        lg = BY_KEY.get(key)
        if not lg:
            raise ValueError("unknown league")
        teams = self._teams(lg)
        q = query.strip().lower()
        if len(q) < 2:
            return []
        hits = [t for t in teams if q in t["name"].lower() or q == t["abbr"].lower()
                or q in t["short"].lower() or q in t["location"].lower()]
        hits.sort(key=lambda t: (not t["abbr"].lower().startswith(q), not t["name"].lower().startswith(q), t["name"]))
        return [{k: t[k] for k in ("id", "abbr", "name", "color")} for t in hits[:limit]]

    def _teams(self, lg):
        path = TEAMS_CACHE.format(lg.key)
        try:
            with open(path) as fh:
                cached = json.load(fh)
            if time.time() - cached["at"] < TEAMS_EVERY and cached["teams"]:
                return cached["teams"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        teams = parse_teams(self._get(f"{SITE}/{lg.path}/teams", dict(lg.params, limit="1000")))
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                json.dump({"at": time.time(), "teams": teams}, fh)
        except OSError:
            pass
        return teams
