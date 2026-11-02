"""Sports screen: ESPN parsing, who's followed, ordering, refresh timing, failures."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import requests
from conftest import FakeResponse

import sports as sp
from render import Renderer
from sample import TEAMS, espn_event, espn_scoreboard
from scroller import Scroller
from test_ticker import FakeDisplay
from ticker import Config, Ticker

NOW = datetime(2026, 11, 7, 23, 0, tzinfo=timezone.utc)       # a Saturday in November
TS = NOW.timestamp()
ALL_ON = {lg.key: sp.Follow(True, lg.default_top, []) for lg in sp.LEAGUES}


def team(key, i):
    return TEAMS[key][i]


def ranking_json(pairs, name="AP Top 25", kind="ap"):
    return {"rankings": [{"id": "2", "name": "Coaches Poll", "type": "usa", "ranks": []},
                         {"id": "1", "name": name, "type": kind,
                          "ranks": [{"current": r, "team": {"id": tid, "abbreviation": "X"}} for tid, r in pairs]}]}


def standings_json(rows):
    """rows: [(team_id, wins, losses)] split over two conferences, nested like ESPN."""
    def entry(tid, w, lo):
        return {"team": {"id": tid, "abbreviation": "X"},
                "stats": [{"name": "wins", "value": w}, {"name": "losses", "value": lo},
                          {"name": "winPercent", "value": w / (w + lo)}]}
    half = len(rows) // 2
    return {"children": [{"name": "Conf A", "standings": {"entries": [entry(*r) for r in rows[:half]]}},
                         {"name": "Conf B", "children": [{"standings": {"entries": [entry(*r) for r in rows[half:]]}}]}]}


class FakeESPN:
    """Routes ESPN URLs to canned JSON; records calls. Values may be exceptions."""

    def __init__(self):
        self.boards = {}       # (league key, YYYYMMDD) -> json
        self.other = {}        # url suffix -> json
        self.calls = []
        self.down = False

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if self.down:
            raise requests.exceptions.ConnectionError("Failed to resolve 'site.api.espn.com'")
        for lg in sp.LEAGUES:
            if url == f"{sp.SITE}/{lg.path}/scoreboard":
                body = self.boards.get((lg.key, (params or {}).get("dates")), espn_scoreboard([]))
                break
        else:
            body = next((v for k, v in self.other.items() if url.endswith(k)), None)
            if body is None:
                return FakeResponse(404, {"code": 404, "message": "not found"})
        if isinstance(body, BaseException):
            raise body
        if isinstance(body, FakeResponse):
            return body
        return FakeResponse(200, body)


def make(follow=None, espn=None, now=TS):
    s = sp.Sports(follow or ALL_ON, clock=lambda: now)
    s.session.get = (espn or FakeESPN()).get
    return s


def day(dt=NOW):
    return dt.astimezone(sp.ET).strftime("%Y%m%d")


def three_games(key, ranked=False):
    t = TEAMS[key]
    return espn_scoreboard([
        espn_event(key, t[0], t[1], "in", NOW - timedelta(hours=1), (14, 10), ranks=(7, None) if ranked else (None, None)),
        espn_event(key, t[2], t[3], "post", NOW - timedelta(hours=5), (31, 28), ranks=(3, 12) if ranked else (None, None)),
        espn_event(key, t[4 % len(t)], t[5 % len(t)], "pre", NOW + timedelta(hours=3), ranks=(1, None) if ranked else (None, None)),
    ])


# --- the league table -----------------------------------------------------------------
def test_league_table():
    keys = [lg.key for lg in sp.LEAGUES]
    assert keys == ["nfl", "ncaaf", "nba", "ncaam", "mlb", "nhl"]
    assert not sp.BY_KEY["nhl"].default_on and all(lg.default_on for lg in sp.LEAGUES if lg.key != "nhl")
    assert {lg.key for lg in sp.LEAGUES if lg.rankings} == {"ncaaf", "ncaam"}
    assert sp.BY_KEY["ncaaf"].default_top == 25 and sp.BY_KEY["nba"].default_top == 8
    for lg in sp.LEAGUES:
        assert lg.months and all(1 <= m <= 12 for m in lg.months) and "/" in lg.path


def test_in_season():
    assert sp.in_season(sp.BY_KEY["nfl"], datetime(2026, 11, 7))
    assert not sp.in_season(sp.BY_KEY["mlb"], datetime(2026, 1, 15))
    assert not sp.in_season(sp.BY_KEY["ncaam"], datetime(2026, 7, 4))


# --- parsing -----------------------------------------------------------------------------
@pytest.mark.parametrize("key", [lg.key for lg in sp.LEAGUES])
def test_parse_live_final_upcoming_every_league(key):
    lg = sp.BY_KEY[key]
    games = sp.parse_scoreboard(three_games(key, lg.rankings), lg)
    assert [g["state"] for g in games] == ["in", "post", "pre"]
    live, final, pre = games
    assert live["away"]["abbr"] == TEAMS[key][0][1] and live["home"]["abbr"] == TEAMS[key][1][1]
    assert (live["away"]["score"], live["home"]["score"]) == (14, 10)
    assert live["detail"] and final["completed"] and pre["away"]["score"] is None
    assert (live["away"]["rank"] == 7) == lg.rankings and live["home"]["rank"] is None   # 99 = unranked
    assert pre["start"] == pytest.approx((NOW + timedelta(hours=3)).timestamp())


def test_parse_skips_odd_events():
    lg = sp.BY_KEY["nfl"]
    good = espn_event("nfl", team("nfl", 0), team("nfl", 1), "in", NOW, (7, 3))
    no_comp = dict(good, competitions=[])
    one_team = dict(good, competitions=[dict(good["competitions"][0], competitors=good["competitions"][0]["competitors"][:1])])
    bad_date = dict(good, date="tomorrow", competitions=[dict(good["competitions"][0], date=None)])
    weird_state = espn_event("nfl", team("nfl", 0), team("nfl", 1), "in", NOW, (7, 3))
    weird_state["competitions"][0]["status"]["type"]["state"] = "postponed"
    no_abbr = espn_event("nfl", team("nfl", 0), team("nfl", 1), "in", NOW, (7, 3))
    del no_abbr["competitions"][0]["competitors"][0]["team"]["abbreviation"]
    games = sp.parse_scoreboard(espn_scoreboard([good, no_comp, one_team, bad_date, weird_state, no_abbr, "junk"]), lg)
    assert len(games) == 1


def test_parse_schedule_style_scores_and_dates():
    ev = espn_event("nba", team("nba", 0), team("nba", 1), "post", NOW, (101, 99))
    for c in ev["competitions"][0]["competitors"]:
        c["score"] = {"value": float(c["score"]), "displayValue": c["score"]}
    ev["date"] = "2026-11-07T23:00:00Z"
    g = sp.parse_event(ev, sp.BY_KEY["nba"])
    assert (g["away"]["score"], g["home"]["score"]) == (101, 99)


@pytest.mark.parametrize("body", [{}, {"events": "none"}, [], "text", None])
def test_parse_changed_format_raises(body):
    with pytest.raises(ValueError):
        sp.parse_scoreboard(body, sp.BY_KEY["nfl"])


def test_parse_rankings_prefers_ap():
    assert sp.parse_rankings(ranking_json([("333", 1), ("2", 14), ("9", 30)])) == {"333": 1, "2": 14}
    for bad in ({}, {"rankings": []}, {"rankings": [{"type": "ap", "ranks": []}]}):
        with pytest.raises(ValueError):
            sp.parse_rankings(bad)


def test_parse_standings_nested():
    table = sp.parse_standings(standings_json([("2", 7, 2), ("15", 3, 6), ("12", 8, 1), ("21", 6, 3)]))
    assert [tid for _, _, tid in table] == ["12", "2", "21", "15"]
    with pytest.raises(ValueError):
        sp.parse_standings({"children": []})


def test_parse_teams_and_colors():
    js = {"sports": [{"leagues": [{"teams": [{"team": {"id": "2", "abbreviation": "BUF", "displayName": "Buffalo Bills",
                                                         "shortDisplayName": "Bills", "location": "Buffalo",
                                                         "color": "00338d"}}, {"team": {}}]}]}]}
    assert sp.parse_teams(js) == [{"id": "2", "abbr": "BUF", "name": "Buffalo Bills", "short": "Bills",
                                   "location": "Buffalo", "color": "00338d"}]
    with pytest.raises(ValueError):
        sp.parse_teams({"sports": []})
    assert sp.team_color({"color": "ffb612"}) == (255, 182, 18)                  # bright: as is
    assert sp.team_color({"color": "0b162a", "alt": "c83803"}) == (200, 56, 3)   # dark: alternate
    dark = sp.team_color({"color": "0b162a", "alt": "000000"})                   # both dark: lightened
    assert max(dark) >= 200


@pytest.mark.parametrize("summary,pct", [("7-2", 7 / 9), ("40-30-5", 40 / 75), ("0-0", None), ("n/a", None)])
def test_win_pct(summary, pct):
    assert sp.win_pct(summary) == (pytest.approx(pct) if pct else None)


# --- refresh, following and ordering ---------------------------------------------------------
def refresh(s):
    s._busy = True
    s._run()
    return s.games()


def test_college_follows_ap_top_25_and_favorites():
    espn = FakeESPN()
    t = TEAMS["ncaaf"]
    espn.boards[("ncaaf", day())] = espn_scoreboard([
        espn_event("ncaaf", t[0], t[1], "in", NOW - timedelta(hours=1), (21, 14)),     # ALA ranked via poll
        espn_event("ncaaf", t[2], t[3], "post", NOW - timedelta(hours=5), (10, 3)),    # neither followed
        espn_event("ncaaf", t[4], t[5], "pre", NOW + timedelta(hours=2)),              # FLA is a favorite
    ])
    espn.other["/college-football/rankings"] = ranking_json([("333", 4), ("61", 30)])
    follow = {"ncaaf": sp.Follow(True, 25, [("57", "FLA")])}
    games = refresh(make(follow, espn))
    assert [(g["state"], g["away"]["abbr"]) for g in games] == [("in", "ALA"), ("pre", "UGA")]
    assert games[1]["fav"] == {"away": False, "home": True}


def test_college_falls_back_to_scoreboard_ranks():
    espn = FakeESPN()
    t = TEAMS["ncaam"]
    espn.boards[("ncaam", day())] = espn_scoreboard([
        espn_event("ncaam", t[0], t[1], "in", NOW, (40, 38), ranks=(9, None)),
        espn_event("ncaam", t[2], t[3], "in", NOW, (40, 38), ranks=(None, None)),
    ])
    espn.other["/mens-college-basketball/rankings"] = FakeResponse(500, {"error": "boom"})
    games = refresh(make({"ncaam": sp.Follow(True, 10, [])}, espn))
    assert [g["away"]["abbr"] for g in games] == ["DUKE"]


def test_pro_follows_best_records():
    espn = FakeESPN()
    t = TEAMS["nfl"]
    espn.boards[("nfl", day())] = espn_scoreboard([
        espn_event("nfl", t[0], t[1], "in", NOW, (7, 3)),       # BUF top-2
        espn_event("nfl", t[4], t[5], "in", NOW, (7, 3)),       # neither in top 2
    ])
    espn.other["/football/nfl/standings"] = standings_json([("2", 8, 1), ("15", 2, 7), ("12", 7, 2), ("6", 1, 8),
                                                             ("9", 3, 6), ("21", 4, 5)])
    games = refresh(make({"nfl": sp.Follow(True, 2, [])}, espn))   # top 2: BUF (8-1) and KC (7-2)
    assert [g["away"]["abbr"] for g in games] == ["BUF"]
    s = make({"nfl": sp.Follow(True, 0, [])}, espn)            # top teams off: nothing followed
    assert refresh(s) == []


def test_pro_falls_back_to_scoreboard_records():
    espn = FakeESPN()
    t = TEAMS["nba"]
    espn.boards[("nba", day())] = espn_scoreboard([
        espn_event("nba", t[0], t[1], "in", NOW, (50, 48), records=("9-1", "2-8")),
        espn_event("nba", t[2], t[3], "in", NOW, (50, 48), records=("3-7", "4-6")),
    ])
    espn.other["/basketball/nba/standings"] = requests.exceptions.ReadTimeout("slow")
    games = refresh(make({"nba": sp.Follow(True, 1, [])}, espn))
    assert [g["away"]["abbr"] for g in games] == ["BOS"]


def test_order_live_then_recent_finals_then_next_game_per_favorite():
    espn = FakeESPN()
    t = TEAMS["nba"]
    yesterday = NOW - timedelta(days=1)
    espn.boards[("nba", day(yesterday))] = espn_scoreboard([
        espn_event("nba", t[0], t[2], "post", NOW - timedelta(hours=20), (100, 90)),   # 17 h ago: shown
        espn_event("nba", t[0], t[3], "post", NOW - timedelta(hours=40), (100, 90)),   # too old
    ])
    espn.boards[("nba", day())] = espn_scoreboard([
        espn_event("nba", t[1], t[0], "post", NOW - timedelta(hours=4), (99, 98)),
        espn_event("nba", t[4], t[5], "in", NOW - timedelta(hours=1), (60, 61)),
        espn_event("nba", t[0], t[1], "in", NOW - timedelta(hours=2), (70, 71)),
    ])
    later = NOW + timedelta(days=2)
    espn.other["/basketball/nba/teams/2/schedule"] = {"events": [
        espn_event("nba", t[0], t[4], "post", NOW - timedelta(days=3), (1, 2)),
        espn_event("nba", t[0], t[5], "pre", later + timedelta(days=1)),
        espn_event("nba", t[3], t[0], "pre", later),
    ]}
    s = make({"nba": sp.Follow(True, 0, [("2", "BOS"), ("7", "DEN")])}, espn)
    games = refresh(s)
    kinds = [(g["state"], g["away"]["abbr"], g["home"]["abbr"]) for g in games]
    assert kinds == [("in", "BOS", "NY"), ("in", "DEN", "DET"),       # live first, by start
                     ("post", "NY", "BOS"), ("post", "BOS", "LAL"),   # finals, newest first
                     ("pre", "GS", "BOS")]                            # BOS's next game (schedule)
    assert s.next_fetch == pytest.approx(TS + sp.LIVE_EVERY)          # a followed game is live


def test_idle_refresh_every_15_minutes():
    espn = FakeESPN()
    t = TEAMS["nfl"]
    espn.boards[("nfl", day())] = espn_scoreboard([espn_event("nfl", t[0], t[1], "post", NOW, (3, 0))])
    s = make({"nfl": sp.Follow(True, 0, [("2", "BUF")])}, espn)
    refresh(s)
    assert s.next_fetch == pytest.approx(TS + sp.IDLE_EVERY)


@pytest.mark.parametrize("key", [lg.key for lg in sp.LEAGUES])
def test_every_league_end_to_end(key):
    """Mocked ESPN for one league: live, final and upcoming all come through and render."""
    lg = sp.BY_KEY[key]
    espn = FakeESPN()
    espn.boards[(key, day())] = three_games(key, lg.rankings)
    tids = [t[0] for t in TEAMS[key]]
    if lg.rankings:
        espn.other[f"/{lg.path}/rankings"] = ranking_json([(tids[0], 7), (tids[2], 3), (tids[4 % len(tids)], 1)])
    else:
        espn.other[f"/{lg.path}/standings"] = standings_json([(tid, 10 - i, i) for i, tid in enumerate(tids)])
    fav = TEAMS[key][5 % len(TEAMS[key])]
    s = make({key: sp.Follow(True, lg.default_top, [(fav[0], fav[1])])}, espn)
    games = refresh(s)
    assert [g["state"] for g in games] == ["in", "post", "pre"]
    for g in games:
        anim = Renderer().game(g, lg, ZoneInfo("America/New_York"), TS)
        assert isinstance(anim, Scroller) and anim.first_frame().getbbox()


def test_out_of_season_and_disabled_leagues_are_not_fetched():
    espn = FakeESPN()
    follow = {lg.key: sp.Follow(lg.key != "nba", lg.default_top, []) for lg in sp.LEAGUES}
    s = make(follow, espn, now=datetime(2026, 7, 4, 18, 0, tzinfo=timezone.utc).timestamp())
    refresh(s)
    fetched = {url.split("/sports/")[1].rsplit("/", 1)[0] for url, _ in espn.calls}
    assert fetched == {"baseball/mlb"}   # July: only MLB is in season (NBA is off anyway)


# --- failures ------------------------------------------------------------------------------
def test_offline_keeps_last_good():
    espn = FakeESPN()
    espn.boards[("nfl", day())] = three_games("nfl")
    s = make({"nfl": sp.Follow(True, 0, [("2", "BUF"), ("12", "KC"), ("6", "DAL")])}, espn)
    good = refresh(s)
    assert len(good) == 3
    espn.down = True
    assert [g["id"] for g in refresh(s)] == [g["id"] for g in good]
    assert s.last_error and "NFL" in s.last_error


def test_offline_from_the_start_shows_nothing():
    espn = FakeESPN()
    espn.down = True
    s = make(None, espn)
    assert refresh(s) == [] and s.last_error


@pytest.mark.parametrize("bad", [FakeResponse(200, text="<html>Service Unavailable</html>"),
                                 FakeResponse(200, {"sports": [], "message": "new format"}),
                                 FakeResponse(503, text="busy")])
def test_malformed_or_changed_format_drops_only_that_league(bad):
    espn = FakeESPN()
    espn.boards[("nfl", day())] = three_games("nfl")
    espn.boards[("nba", day())] = bad
    follow = {"nfl": sp.Follow(True, 0, [("2", "BUF")]), "nba": sp.Follow(True, 8, [("2", "BOS")])}
    games = refresh(make(follow, espn))
    assert {g["league"] for g in games} == {"nfl"}


def test_empty_scoreboard():
    s = make(None, FakeESPN())
    assert refresh(s) == []


# --- favorites search -------------------------------------------------------------------------
def test_team_search(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "TEAMS_CACHE", str(tmp_path / "teams-{}.json"))
    espn = FakeESPN()
    espn.other["/football/nfl/teams"] = {"sports": [{"leagues": [{"teams": [
        {"team": {"id": i, "abbreviation": a, "displayName": n, "shortDisplayName": n.split()[-1],
                  "location": " ".join(n.split()[:-1]), "color": c}} for i, a, n, c, _ in TEAMS["nfl"]]}]}]}
    s = make(None, espn)
    assert [t["abbr"] for t in s.search_teams("nfl", "bil")] == ["BUF"]
    assert [t["abbr"] for t in s.search_teams("nfl", "kc")] == ["KC"]
    assert s.search_teams("nfl", "a") == []
    calls = len(espn.calls)
    s.search_teams("nfl", "dolph")            # served from the cache file
    assert len(espn.calls) == calls
    with pytest.raises(ValueError):
        s.search_teams("cricket", "x")


# --- on the panel --------------------------------------------------------------------------------
def test_render_marks_favorites_ranks_and_scrolls_long_lines():
    g = sp.parse_scoreboard(three_games("ncaaf", True), sp.BY_KEY["ncaaf"])[0]
    g["fav"] = {"away": True, "home": False}
    r = Renderer()
    a = r.game(g, sp.BY_KEY["ncaaf"], None, TS)
    img = a.first_frame()
    assert any(img.getpixel((x, y)) == Renderer.STAR for x in range(64) for y in range(32))
    g["away"]["abbr"], g["away"]["rank"], g["away"]["score"] = "WASH", 25, 105
    a = r.game(g, sp.BY_KEY["ncaaf"], None, TS)
    assert a.scrolls and any(lane.box[1] == 9 for lane in a.lanes)   # the away line scrolls


@pytest.mark.parametrize("state,offset,want", [("in", 0, "5:12 3RD"), ("post", 0, "FINAL"),
                                                ("pre", 3, "9:00P"), ("pre", 26, "SUN 8:00P"),   # 23:00 UTC = 6 PM ET
                                                ("pre", 24 * 9, "11/16 6:00P")])
def test_status_text(state, offset, want):
    ev = espn_event("nfl", team("nfl", 0), team("nfl", 1), state, NOW + timedelta(hours=offset), (7, 3))
    g = sp.parse_event(ev, sp.BY_KEY["nfl"])
    text, _ = Renderer.game_status(g, ZoneInfo("America/New_York"), TS)
    assert text == want


def test_ticker_screen_per_game_and_stale_marker():
    cfg = Config()
    cfg.screens = ["sports"]
    t = Ticker(cfg, FakeDisplay())
    t.sports.next_fetch = float("inf")
    games = sp.parse_scoreboard(three_games("nfl"), sp.BY_KEY["nfl"])
    t.sports.games = lambda now=None: [dict(g, fav={}, fetched=now - 600) for g in games]
    shots = [s() for s in t.screens()]
    assert len(shots) == 3 and all(isinstance(a, Scroller) for a in shots)
    px = shots[0].first_frame().getpixel((63, 31))     # live data 10 min old: stale
    assert px == (230, 40, 30)
    assert shots[1].first_frame().getpixel((63, 31)) != (230, 40, 30)   # finals: fine


def test_follow_settings():
    env = {"SPORTS_NHL": "on", "SPORTS_NFL": "off", "SPORTS_NBA_TOP": "4", "SPORTS_MLB_TOP": "7",
           "SPORTS_NCAAF_FAVORITES": "333:ALA, 57:fla, bad, :X"}
    f = sp.follow_from_env(env)
    assert f["nhl"].on and not f["nfl"].on and f["ncaam"].on
    assert f["nba"].top == 4 and f["mlb"].top == 8                     # 7 isn't a choice: default
    assert f["ncaaf"].favorites == [("333", "ALA"), ("57", "FLA")]
