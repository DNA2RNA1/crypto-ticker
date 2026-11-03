"""Made-up data for previews, the emulator and screenshots (no network needed)."""

import math
import random


def sample_assets(symbols):
    """Made-up prices and a random-walk 7-day sparkline, for layout testing only."""
    known = {
        "btc": ("bitcoin", 109234.56), "eth": ("ethereum", 4012.37),
        "ada": ("cardano", 0.8123), "sol": ("solana", 212.44),
        "doge": ("dogecoin", 0.2471), "xrp": ("ripple", 2.913),
        "link": ("chainlink", 22.41), "hbar": ("hedera-hashgraph", 0.2412),
        "pepe": ("pepe", 0.00001123), "snek": ("snek", 0.00341), "night": ("midnight-3", 0.02696),
    }
    rng = random.Random(7)
    out = []
    for sym in symbols:
        sym, _, pinned = sym.lower().partition(":")
        coin_id, price = known.get(sym, (pinned or sym, 1.2345))
        drift = rng.uniform(-0.12, 0.12)
        walk, v = [], price * (1 - drift)
        for i in range(168):
            v *= 1 + rng.gauss(drift / 168, 0.006) + 0.004 * math.sin(i / 9)
            walk.append(v)
        walk = [x * price / walk[-1] for x in walk]  # end exactly at the current price
        out.append({"symbol": sym.upper(), "id": coin_id, "name": coin_id,
                    "price": price, "change_24h": rng.uniform(-6, 6),
                    "sparkline": walk, "image": None})
    return out


def apply_sample(ticker):
    """Fill a Ticker with sample prices and feed data, and stop it fetching anything."""
    ticker.assets = sample_assets(ticker.cfg.symbols)
    ticker.next_fetch = float("inf")
    up = [100 + i * 0.05 + (i % 5) * 0.03 for i in range(24)]
    down = [100 - i * 0.03 + (i % 4) * 0.02 for i in range(24)]
    ticker.index_rows = [{"label": "S&P", "change": 0.42, "abs": 3.24, "unit": "$", "spark": up},
                         {"label": "NAS", "change": -0.31, "abs": -2.31, "unit": "$", "spark": down}]
    for feed in (ticker.yahoo_dow, ticker.yahoo, ticker.fear_greed, ticker.weather):
        feed.next_fetch = float("inf")
    dow = [45140 * v / 100 for v in up]
    ticker.yahoo_dow.data = [{"label": "DOW", "change": (dow[-1] - 45140) / 45140 * 100,
                              "points": dow[-1] - 45140, "intraday": dow,
                              "price": dow[-1], "prev_close": 45140.0}]
    ticker.fear_greed.data = {"value": 70, "label": "Greed", "yesterday": 65,
                              "history": [60, 58, 62, 66, 64, 63, 65, 70]}
    ticker.weather.data = sample_weather(code=0, is_day=False)  # the clock screenshots are at night


SAMPLE_NEWS = [  # made-up headlines and sources, for previews only
    {"title": "Bitcoin Holds Above $109K as Spot ETF Inflows Reach a Three-Week High", "source": "CRYPTO WIRE"},
    {"title": "Stocks Edge Higher as Tech Rallies Ahead of Earnings", "source": "MARKETS"},
    {"title": "Stablecoin Supply Hits a New Record", "source": "CHAIN DAILY"},
]


def sample_news(now_ts, ages=(25 * 60, 2 * 3600, 5 * 3600)):
    return [dict(item, at=now_ts - age, link=None) for item, age in zip(SAMPLE_NEWS, ages)]


SAMPLE_VIDEOS = [  # made-up channels and titles
    {"title": "Bitcoin: The Risk Nobody Is Talking About", "source": "MARKET CYCLE"},
    {"title": "Altcoin Season and the Business Cycle", "source": "CHART TALK"},
]


def sample_videos(now_ts, ages=(3 * 3600, 26 * 3600)):
    return [dict(item, at=now_ts - age, link=None) for item, age in zip(SAMPLE_VIDEOS, ages)]


def sample_weather(code=2, is_day=True):
    """Made-up conditions: 76.8 now, a warm afternoon then cooling into the evening."""
    hourly = [76.8, 79.1, 81.0, 82.4, 83.0, 82.1, 80.2, 77.5, 74.6, 72.0, 70.3, 69.1, 68.4]
    return {"temp": 76.8, "code": code, "is_day": is_day, "high": 83.4, "low": 66.2, "hourly": hourly}


# --- sports: ESPN-shaped JSON, so tests and screenshots go through the real parser ---------
TEAMS = {  # league -> [(id, abbreviation, name, color, alternate color)]
    "nfl": [("2", "BUF", "Buffalo Bills", "00338d", "d50a0a"), ("15", "MIA", "Miami Dolphins", "008e97", "fc4c02"),
            ("12", "KC", "Kansas City Chiefs", "e31837", "ffb612"), ("21", "PHI", "Philadelphia Eagles", "06424d", "a5acaf"),
            ("6", "DAL", "Dallas Cowboys", "002a5c", "b0b7bc"), ("9", "GB", "Green Bay Packers", "204e32", "ffb612")],
    "ncaaf": [("333", "ALA", "Alabama Crimson Tide", "9e1b32", "ffffff"), ("2", "AUB", "Auburn Tigers", "002b5c", "f26522"),
              ("194", "OSU", "Ohio State Buckeyes", "ba0c2f", "a7b1b7"), ("130", "MICH", "Michigan Wolverines", "00274c", "ffcb05"),
              ("61", "UGA", "Georgia Bulldogs", "ba0c2f", "000000"), ("57", "FLA", "Florida Gators", "0021a5", "fa4616")],
    "nba": [("2", "BOS", "Boston Celtics", "008348", "ffffff"), ("18", "NY", "New York Knicks", "1d428a", "f58426"),
            ("13", "LAL", "Los Angeles Lakers", "552583", "fdb927"), ("9", "GS", "Golden State Warriors", "1d428a", "ffc72c"),
            ("7", "DEN", "Denver Nuggets", "0e2240", "fec524"), ("8", "DET", "Detroit Pistons", "1d42ba", "c8102e")],
    "ncaam": [("150", "DUKE", "Duke Blue Devils", "00539b", "ffffff"), ("153", "UNC", "North Carolina Tar Heels", "7bafd4", "13294b"),
              ("96", "UK", "Kentucky Wildcats", "0033a0", "ffffff"), ("2305", "KU", "Kansas Jayhawks", "0051ba", "e8000d"),
              ("41", "CONN", "UConn Huskies", "000e2f", "ffffff"), ("2509", "PUR", "Purdue Boilermakers", "000000", "cfb991")],
    "mlb": [("10", "NYY", "New York Yankees", "132448", "c4ced4"), ("2", "BOS", "Boston Red Sox", "0d2b56", "bd3039"),
            ("19", "LAD", "Los Angeles Dodgers", "005a9c", "ef3e42"), ("26", "SF", "San Francisco Giants", "fd5a1e", "27251f"),
            ("16", "CHC", "Chicago Cubs", "0e3386", "cc3433"), ("30", "STL", "St. Louis Cardinals", "be0a14", "0c2340")],
    "nhl": [("6", "BOS", "Boston Bruins", "231f20", "fdb927"), ("10", "TOR", "Toronto Maple Leafs", "003e7e", "ffffff"),
            ("26", "FLA", "Florida Panthers", "c8102e", "041e42"), ("25", "EDM", "Edmonton Oilers", "00205b", "ff4c00")],
}
SCORES = {"nfl": ((24, 17), (31, 28)), "ncaaf": ((24, 17), (31, 28)), "nba": ((88, 91), (104, 99)),
          "ncaam": ((61, 58), (77, 70)), "mlb": ((3, 5), (6, 2)), "nhl": ((2, 3), (4, 1))}
LIVE_DETAIL = {"nfl": "5:12 - 3rd", "ncaaf": "2:41 - 4th", "nba": "7:45 - 3rd", "ncaam": "12:30 - 2nd",
               "mlb": "Top 7th", "nhl": "8:14 - 2nd"}


def espn_competitor(team, home, score=None, rank=None, record=None, winner=False):
    tid, abbr, name, color, alt = team
    c = {"id": tid, "homeAway": "home" if home else "away", "winner": winner,
         "team": {"id": tid, "abbreviation": abbr, "displayName": name, "shortDisplayName": name.split()[-1],
                  "color": color, "alternateColor": alt, "logo": f"https://a.espncdn.com/i/teamlogos/{tid}.png"},
         "curatedRank": {"current": rank or 99},
         "records": [{"name": "overall", "type": "total", "summary": record or "0-0"}]}
    if score is not None:
        c["score"] = str(score)
    return c


def espn_event(league, away, home, state, start, scores=(None, None), ranks=(None, None),
               records=("6-2", "5-3"), detail=None, eid=None, odds=None):
    """One ESPN scoreboard event. ``start`` is a UTC datetime."""
    names = {"pre": ("STATUS_SCHEDULED", "Scheduled"), "in": ("STATUS_IN_PROGRESS", "In Progress"),
             "post": ("STATUS_FINAL", "Final")}
    short = detail or {"in": LIVE_DETAIL.get(league, "Live"), "post": "Final",
                       "pre": start.strftime("%a, %B %d at %I:%M %p EST")}[state]
    status = {"clock": 0.0, "displayClock": "0:00", "period": 3,
              "type": {"id": {"pre": "1", "in": "2", "post": "3"}[state], "name": names[state][0], "state": state,
                       "completed": state == "post", "description": names[state][1], "detail": short,
                       "shortDetail": short}}
    sa, sh = scores if state != "pre" else (None, None)
    win = None if state != "post" or sa is None else ("home" if sh > sa else "away")
    comp = {"id": eid or f"{league}-{away[0]}-{home[0]}-{start:%Y%m%d}", "date": start.strftime("%Y-%m-%dT%H:%MZ"),
            "status": status,
            "competitors": [espn_competitor(home, True, sh, ranks[1], records[1], win == "home"),
                            espn_competitor(away, False, sa, ranks[0], records[0], win == "away")]}
    if odds:
        comp["odds"] = [odds]
    return {"id": comp["id"], "date": comp["date"], "name": f"{away[2]} at {home[2]}",
            "shortName": f"{away[1]} @ {home[1]}", "status": status, "competitions": [comp]}


def espn_scoreboard(events):
    return {"leagues": [{"id": "1", "name": "League"}], "season": {"type": 2, "year": 2026}, "events": events}


# made-up lines in ESPN's scoreboard format: (home spread, total, away ML, home ML)
LINES = {"nfl": (-3.5, 47.5, 145, -170), "ncaaf": (-14, 55.5, 600, -900), "nba": (-6.5, 228.5, 210, -250),
         "ncaam": (-8, 141.5, 320, -400), "mlb": (-1.5, 8.5, 120, -140), "nhl": (-1.5, 6.5, 110, -130)}


def sample_odds(league, home):
    spread, total, away_ml, home_ml = LINES[league]
    return {"provider": {"id": "1", "name": "Sample"}, "details": f"{home[1]} {spread:g}", "overUnder": total,
            "spread": spread, "awayTeamOdds": {"favorite": False, "moneyLine": away_ml},
            "homeTeamOdds": {"favorite": True, "moneyLine": home_ml}}


SAMPLE_MARKETS = [  # made-up questions and odds, for previews only
    {"source": "poly", "title": "Which team wins the championship?", "closed": False,
     "outcomes": [("Lakers", 0.31), ("Celtics", 0.24), ("Thunder", 0.18)]},
    {"source": "kalshi", "title": "Will the Fed cut rates at the next meeting?", "closed": False,
     "outcomes": [("Yes", 0.73), ("No", 0.27)]},
]


def sample_markets(now_ts):
    return [dict(m, entry=f"{m['source']}:sample-{i}", fetched=now_ts, stale=False)
            for i, m in enumerate(SAMPLE_MARKETS)]


def sample_games(now=None):
    """Parsed games for the screenshots: one live, one final, one upcoming per league."""
    from datetime import datetime, timedelta, timezone
    from sports import BY_KEY, parse_scoreboard
    now = now or datetime(2026, 11, 7, 23, 0, tzinfo=timezone.utc)
    out = {}
    for key, teams in TEAMS.items():
        lg = BY_KEY[key]
        ranked = lg.rankings
        events = [
            espn_event(key, teams[0], teams[1], "in", now - timedelta(hours=1), SCORES[key][0],
                       ranks=(7, None) if ranked else (None, None)),
            espn_event(key, teams[2], teams[3], "post", now - timedelta(hours=6), SCORES[key][1],
                       ranks=(3, 12) if ranked else (None, None), detail="Final/OT" if key == "nba" else None),
            espn_event(key, teams[4] if len(teams) > 4 else teams[0], teams[5] if len(teams) > 5 else teams[1],
                       "pre", now + timedelta(days=1, hours=2), ranks=(1, None) if ranked else (None, None),
                       odds=sample_odds(key, teams[5] if len(teams) > 5 else teams[1])),
        ]
        out[key] = parse_scoreboard(espn_scoreboard(events), lg)
    return out


def sample_watchlist(now_ts):
    """Made-up watchlist quotes (a stock, a future, a mutual fund, a closed market)."""
    def day(start, end, n=60, wiggle=0.0012, seed=3):
        rng = random.Random(seed)
        walk, v = [], 0.0
        for _ in range(n):
            v += rng.gauss(0, wiggle)
            walk.append(v)
        # a random walk bent so it starts at ``start`` and ends exactly at ``end``
        return [start + (end - start) * i / (n - 1) + start * (w - walk[-1] * i / (n - 1))
                for i, w in enumerate(walk)]

    def q(label, symbol, kind, price, prev, cur, pts, closed=False, span=None, name=""):
        row = {"symbol": symbol, "label": label, "kind": kind, "name": name, "price": price,
               "prev_close": prev, "change": (price - prev) / prev * 100, "points": price - prev,
               "currency": cur, "intraday": pts, "traded": now_ts - (5 * 3600 if closed else 120),
               "closed": closed, "fetched": now_ts, "stale": False}
        if span:
            row["span"] = span
        return row

    return [
        q("AAPL", "AAPL", "EQUITY", 231.47, 228.12, "usd", day(228.4, 231.47, seed=1), name="Apple Inc."),
        q("S&P", "ES=F", "FUTURE", 6012.25, 6031.50, "", day(6030, 6012.25, seed=2)),
        q("WTI", "CL=F", "FUTURE", 71.38, 70.62, "", day(70.7, 71.38, seed=4), closed=True),
        q("VTSAX", "VTSAX", "MUTUALFUND", 148.32, 147.61, "usd",
          day(141.0, 148.32, n=22, wiggle=0.003, seed=5), span="1M"),
        q("CORN", "ZC=F", "FUTURE", 428.75, 431.0, "", day(431, 428.75, seed=6)),
    ]
