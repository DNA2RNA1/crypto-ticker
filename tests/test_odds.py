"""Odds (display only): ESPN pre-game lines on the sports screen, and the prediction-market
screen for Polymarket and Kalshi, with mocked responses (errors, timeouts, odd formats)."""

import json

import pytest
import requests

import markets as mk
from render import Renderer
from sample import espn_event, espn_scoreboard, sample_games, sample_markets, sample_odds
from sports import BY_KEY, parse_odds, parse_scoreboard
from test_ticker import FakeDisplay
from ticker import Config, Ticker

BUF, KC = {"abbr": "BUF"}, {"abbr": "KC"}


# --- ESPN lines -------------------------------------------------------------------
@pytest.mark.parametrize("details,expect", [
    ("KC -3.5", {"home": "-3.5", "away": "+3.5"}),
    ("BUF -7", {"away": "-7", "home": "+7"}),
    ("EVEN", {"away": "PK", "home": "PK"}),
    ("PK", {"away": "PK", "home": "PK"}),
    ("KC 0", {"away": "PK", "home": "PK"}),
    ("NYJ -3", None),                 # a team that isn't playing: no spread rather than a wrong one
    ("KC -3.5 (-110)", None),
    ("", None),
])
def test_spread_from_details(details, expect):
    o = parse_odds({"odds": [{"details": details, "overUnder": 47.5}]}, BUF, KC)
    assert o["spread"] == expect and o["total"] == "47.5"


def test_moneylines_both_formats():
    old = {"homeTeamOdds": {"moneyLine": -170}, "awayTeamOdds": {"moneyLine": 145}}
    new = {"moneyline": {"home": {"close": {"odds": "-170"}}, "away": {"open": {"odds": "+145"}}}}
    for odds in (old, new):
        assert parse_odds({"odds": [odds]}, BUF, KC)["ml"] == {"away": "+145", "home": "-170"}
    even = {"moneyline": {"home": {"close": {"odds": "EVEN"}}, "away": {"close": {"odds": "EVEN"}}}}
    assert parse_odds({"odds": [even]}, BUF, KC)["ml"] == {"away": "EVEN", "home": "EVEN"}


@pytest.mark.parametrize("comp", [
    {}, {"odds": None}, {"odds": []}, {"odds": ["x"]}, {"odds": [{"details": "weird", "overUnder": "n/a"}]},
    {"odds": [{"homeTeamOdds": {"moneyLine": 5}, "awayTeamOdds": {"moneyLine": True}}]},
    {"odds": [{"overUnder": float("nan")}]},
])
def test_no_usable_line(comp):
    assert parse_odds(comp, BUF, KC) is None


def test_only_pre_games_carry_odds():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 7, 23, 0, tzinfo=timezone.utc)
    teams = (("1", "BUF", "Buffalo", "00338d", "c60c30"), ("2", "KC", "Kansas City", "e31837", "ffb612"))
    lines = sample_odds("nfl", teams[1])
    games = parse_scoreboard(espn_scoreboard([
        espn_event("nfl", *teams, "pre", now, odds=lines),
        espn_event("nfl", *teams, "in", now, (7, 3), odds=lines, eid="live")]), BY_KEY["nfl"])
    pre, live = games
    assert pre["odds"]["spread"] == {"home": "-3.5", "away": "+3.5"} and live["odds"] is None


def test_broken_odds_never_drop_the_game(monkeypatch):
    import sports
    monkeypatch.setattr(sports, "parse_odds", lambda *a: (_ for _ in ()).throw(TypeError("odd")))
    games = sample_games()
    assert all(g["odds"] is None for g in games["nfl"]) and len(games["nfl"]) == 3


def pre_game(key="nfl"):
    return next(g for g in sample_games()[key] if g["state"] == "pre")


@pytest.mark.parametrize("key", ["nfl", "ncaaf", "nba", "ncaam", "mlb", "nhl"])
def test_odds_frame(key):
    r = Renderer()
    img = r.game_odds(pre_game(key), BY_KEY[key])
    assert img.size == (64, 32) and img.crop((40, 9, 64, 32)).getbbox()   # numbers on the right
    assert img.crop((30, 0, 64, 8)).getbbox()                               # O/U in the header
    assert img.tobytes() != r.game(pre_game(key), BY_KEY[key]).first_frame().tobytes()


def test_baseball_shows_moneyline_football_spread():
    r = Renderer()
    nfl, mlb = pre_game("nfl"), pre_game("mlb")
    assert r.game_odds(nfl, BY_KEY["nfl"]).tobytes() != r.game_odds(
        dict(nfl, odds=dict(nfl["odds"], spread=None)), BY_KEY["nfl"]).tobytes()      # falls back to ML
    no_ml = dict(mlb, odds=dict(mlb["odds"], ml=None))
    assert r.game_odds(no_ml, BY_KEY["mlb"]).tobytes() != r.game_odds(mlb, BY_KEY["mlb"]).tobytes()
    assert r.game_odds(dict(nfl, odds=None), BY_KEY["nfl"]) is None


def sports_ticker(odds_on):
    cfg = Config()
    cfg.screens = ["sports"]
    cfg.sports_odds = odds_on
    t = Ticker(cfg, FakeDisplay())
    games = [g for gs in sample_games().values() for g in gs]
    for g in games:
        g["fetched"] = 9e12
    t.sports.update = lambda now=None: None
    t.sports.games = lambda now=None: games
    return t, games


def test_odds_toggle():
    off, games = sports_ticker(False)
    on, _ = sports_ticker(True)
    with_odds = sum(1 for g in games if g["state"] == "pre" and g["odds"])
    assert with_odds and len(on.screens()) == len(off.screens()) + with_odds
    flips = [s() for s in on.screens()]
    flips = [x for x in flips if isinstance(x, tuple)]
    assert len(flips) == with_odds and all(x[2] is False for x in flips)    # in place, no transition


# --- prediction-market links --------------------------------------------------------
@pytest.mark.parametrize("text,entry", [
    ("https://polymarket.com/event/nba-champion-2027", "poly:nba-champion-2027"),
    ("polymarket.com/event/Fed-Decision-In-December/will-the-fed-cut?tid=1", "poly:fed-decision-in-december"),
    ("poly:some-event", "poly:some-event"),
    ("https://kalshi.com/markets/kxfeddecision/fed-meeting/KXFEDDECISION-26DEC", "kalshi:KXFEDDECISION-26DEC"),
    ("kalshi:kxnba-27", "kalshi:KXNBA-27"),
])
def test_parse_entry(text, entry):
    assert mk.parse_entry(text) == entry


@pytest.mark.parametrize("bad", ["https://example.com/event/x", "polymarket.com/markets", "poly:", "kalshi:a b",
                                 "javascript:alert(1)", "https://kalshi.com/", "poly:<script>"])
def test_bad_entries(bad):
    with pytest.raises(ValueError):
        mk.parse_entry(bad)


def test_entries_limits():
    assert mk.parse_entries("poly:a\npoly:a, kalshi:kb") == ["poly:a", "kalshi:KB"]
    with pytest.raises(ValueError):
        mk.parse_entries(",".join(f"poly:e{i}" for i in range(11)))
    assert mk.entries_from_env("poly:a,https://example.com/x,kalshi:kb") == ["poly:a", "kalshi:KB"]


# --- parsing ------------------------------------------------------------------------
def poly_binary():
    return [{"title": "Will it snow on New Year's Day?", "closed": False,
             "markets": [{"question": "Will it snow?", "outcomes": '["Yes", "No"]',
                          "outcomePrices": '["0.355", "0.645"]', "closed": False, "active": True}]}]


def poly_multi():
    def m(name, yes, closed=False):
        return {"question": f"Will {name} win?", "groupItemTitle": name, "outcomes": '["Yes", "No"]',
                "outcomePrices": json.dumps([str(yes), str(1 - yes)]), "closed": closed, "active": not closed}
    return [{"title": "Champion 2027", "markets": [m("Lakers", 0.31), m("Celtics", 0.24), m("Knicks", 0.0, True),
                                                   m("Thunder", 0.18), m("Heat", 0.02), m("Bulls", 0.01)]}]


def test_polymarket_binary():
    m = mk.parse_polymarket(poly_binary())
    assert m["title"] == "Will it snow on New Year's Day?" and m["outcomes"] == [("No", 0.645), ("Yes", 0.355)]


def test_polymarket_multi_sorted_open_only():
    m = mk.parse_polymarket(poly_multi())
    assert [n for n, _ in m["outcomes"]] == ["Lakers", "Celtics", "Thunder", "Heat"]   # top 4, closed skipped


@pytest.mark.parametrize("data", [[], {}, None, "x", [{"title": "t", "markets": []}],
                                  [{"markets": [{"outcomes": "not json", "outcomePrices": "[1"}]}],
                                  [{"markets": [{"outcomes": '["Yes"]', "outcomePrices": '["1.7"]'}]}]])
def test_polymarket_malformed(data):
    with pytest.raises(ValueError):
        mk.parse_polymarket(data)


def test_kalshi_single_market_price_formats():
    for market in ({"title": "Fed cut?", "last_price": 73, "status": "active"},
                   {"title": "Fed cut?", "last_price_dollars": "0.7300", "status": "active"},
                   {"title": "Fed cut?", "yes_bid": 72, "yes_ask": 74, "status": "active"},
                   {"title": "Fed cut?", "yes_bid_dollars": "0.72", "yes_ask_dollars": "0.74"}):
        m = mk.parse_kalshi({"market": market})
        assert m["title"] == "Fed cut?" and m["outcomes"][0] == ("Yes", pytest.approx(0.73))
        assert m["outcomes"][1] == ("No", pytest.approx(0.27))


def test_kalshi_event_with_nested_markets():
    data = {"event": {"title": "NBA Champion", "markets": [
        {"yes_sub_title": "Lakers", "last_price": 31, "status": "active"},
        {"yes_sub_title": "Celtics", "last_price": 24, "status": "active"},
        {"yes_sub_title": "Knicks", "last_price": 99, "status": "settled"},
        {"yes_sub_title": "No trades yet"}]}}
    m = mk.parse_kalshi(data)
    assert m["outcomes"] == [("Lakers", 0.31), ("Celtics", 0.24)] and not m["closed"]


@pytest.mark.parametrize("data", [None, [], {}, {"event": {"markets": []}}, {"market": {"title": "x"}},
                                  {"market": {"last_price": "abc"}}, {"market": {"last_price": 250}}])
def test_kalshi_malformed(data):
    with pytest.raises(ValueError):
        mk.parse_kalshi(data)


# --- the feed with mocked HTTP --------------------------------------------------------
class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def raise_for_status(self):
        if self.status_code != 200:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class Session:
    def __init__(self, answers):
        self.answers, self.calls, self.headers = answers, [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        key = "/".join(url.split("/")[-2:]) if "kalshi" in url else (params or {}).get("slug")
        a = self.answers.get(key, (404, {}))
        if isinstance(a, Exception):
            raise a
        return Resp(*a)


def feed(entries, answers):
    f = mk.Markets(mk.parse_entries(entries))
    f.session = Session(answers)
    return f


def test_fetch_mixed_sources_and_failures():
    import requests
    f = feed("poly:champ,poly:snow,kalshi:KXFED,poly:gone,poly:slow,poly:html",
             {"champ": (200, poly_multi()), "snow": (200, poly_binary()), "markets/KXFED": (200, {"market": {
                 "title": "Fed cut?", "last_price": 73}}), "gone": (200, []),
              "slow": requests.Timeout("read timed out"), "html": (200, ValueError("Expecting value"))})
    rows = f.fetch()
    assert [r["entry"] for r in rows] == ["poly:champ", "poly:snow", "kalshi:KXFED"]
    st = f.status_for(f.entries)
    assert st["poly:champ"]["ok"] and st["poly:champ"]["title"] == "Champion 2027"
    assert not st["poly:gone"]["ok"] and "no such" in st["poly:gone"]["error"]
    assert "timed out" in st["poly:slow"]["error"] and not st["poly:html"]["ok"]
    # an event ticker 404 falls back to the market endpoint
    assert any("/events/KXFED" in u for u in f.session.calls) and any("/markets/KXFED" in u for u in f.session.calls)


def test_rate_limited_and_offline_keep_last_good():
    import requests
    f = feed("poly:champ", {"champ": (200, poly_multi())})
    f.fetch()
    f.session = Session({"champ": (429, None)})
    (row,) = f.fetch()
    assert row["stale"] and row["outcomes"][0][0] == "Lakers" and f.status["poly:champ"]["had"]
    f.session = Session({"champ": requests.ConnectionError("Network is unreachable")})
    assert f.fetch()[0]["stale"]


def test_nothing_loaded_raises_and_run_survives():
    f = feed("poly:x", {})
    with pytest.raises(ValueError):
        f.fetch()
    f._run()
    assert f.data is None


# --- screen -------------------------------------------------------------------------
def test_market_screen():
    r = Renderer()
    for m in sample_markets(0):
        sc = r.market(m)
        first = sc.first_frame()
        assert first.size == (64, 32) and sc.scrolls                     # the question scrolls
        assert first.crop((40, 9, 64, 32)).getbbox()                     # the percentages
    long_name = dict(sample_markets(0)[0], outcomes=[("Oklahoma City Thunder", 0.4), ("No", 0.004)])
    assert r.market(long_name).scrolls
    assert r.market(dict(sample_markets(0)[1], outcomes=[])).first_frame().getbbox()
    stale = r.market(dict(sample_markets(0)[1], stale=True), stale=True).first_frame()
    assert stale.getpixel((63, 31)) == (230, 40, 30)


def test_odds_screen_in_ticker():
    cfg = Config()
    cfg.screens = ["odds"]
    cfg.odds_per_cycle = 1
    t = Ticker(cfg, FakeDisplay())
    t.markets.update = lambda now=None: None
    t.markets.rows = lambda now=None: sample_markets(0)
    a, b = t.screens(), t.screens()
    assert len(a) == len(b) == 1 and a[0]().first_frame().tobytes() != b[0]().first_frame().tobytes()


def test_settings(monkeypatch):
    c = Config.from_env()
    assert not c.sports_odds and c.odds_markets == [] and "odds" not in c.screens   # off by default
    monkeypatch.setenv("SPORTS_ODDS", "on")
    monkeypatch.setenv("ODDS_MARKETS", "poly:a,https://example.com/x,kalshi:kxb")
    monkeypatch.setenv("ODDS_PER_CYCLE", "40")
    c = Config.from_env()
    assert c.sports_odds and c.odds_markets == ["poly:a", "kalshi:KXB"] and c.odds_per_cycle == 10


def test_settings_page(app):
    s = app.settings()
    assert s["sports_odds"] == "off" and s["odds_markets"] == [] and "odds" in s["screen_names"]
    saved = app.save({"sports_odds": "on", "odds_per_cycle": 2,
                      "odds_markets": "https://polymarket.com/event/champ-2027\nkalshi.com/markets/kxfed/x/KXFED-26DEC"})
    assert saved == {"SPORTS_ODDS": "on", "ODDS_MARKETS": "poly:champ-2027,kalshi:KXFED-26DEC", "ODDS_PER_CYCLE": "2"}
    assert app.settings()["odds_status"] == {"poly:champ-2027": None, "kalshi:KXFED-26DEC": None}
    for bad in ({"sports_odds": "maybe"}, {"odds_markets": "https://example.com/bet"}, {"odds_per_cycle": 11}):
        with pytest.raises(ValueError):
            app.save(bad)
