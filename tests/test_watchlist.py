"""Watchlist: Yahoo stocks, ETFs, mutual funds and futures with mocked chart responses
(rate limits, timeouts, malformed data, Wi-Fi loss), the quote screen and settings."""

import time

import pytest

import feeds
import watchlist as wl
from render import DOWN, SYMBOL, UP, Renderer
from test_metals import FakeSession, Resp
from test_ticker import FakeDisplay
from ticker import Config, Ticker

NOW = 1_780_000_000.0


@pytest.fixture(autouse=True)
def no_real_curl(monkeypatch):
    monkeypatch.setattr(feeds.shutil, "which", lambda name: None)


def chart(price, prev, kind="EQUITY", currency="USD", closes=(100, 101, 102, 101.5), traded=NOW - 60,
          name="Some Company Inc."):
    meta = {"regularMarketPrice": price, "chartPreviousClose": prev, "instrumentType": kind,
            "currency": currency, "regularMarketTime": traded, "shortName": name}
    return {"chart": {"result": [{"meta": meta, "indicators": {"quote": [{"close": list(closes)}]}}],
                      "error": None}}


NOT_FOUND = {"chart": {"result": None, "error": {"code": "Not Found",
                                                 "description": "No data found, symbol may be delisted"}}}


class ParamSession(FakeSession):
    """Like FakeSession, but a mutual fund answers its 1-month request separately."""
    def __init__(self, answers, month=None, default=404):
        super().__init__(answers, default)
        self.month = month or {}

    def get(self, url, params=None, timeout=None):
        if params and params.get("range") == "1mo":
            symbol = url.rsplit("/", 1)[1]
            self.calls.append(("1mo", symbol))
            status, data = self.month.get(symbol, (404, None))
            return Resp(status, data)
        return super().get(url, params, timeout)


def make(entries, answers, month=None, default=404):
    f = wl.Watchlist(wl.parse_entries(entries))
    f.session = ParamSession(answers, month, default)
    return f


# --- entries ----------------------------------------------------------------------
def test_parse_entries():
    assert wl.parse_entries("aapl, es=f:s&p\nVTSAX,^vix, DX-Y.NYB") == [
        ("AAPL", "AAPL"), ("ES=F", "S&P"), ("VTSAX", "VTSAX"), ("^VIX", "VIX"), ("DX-Y.NYB", "DX-Y")]
    assert wl.parse_entries("AAPL,aapl:APL") == [("AAPL", "AAPL")]      # duplicates: first wins
    assert wl.parse_entries("") == []
    assert wl.default_label("BRK-B") == "BRK-B" and wl.default_label("CL=F") == "CL"


@pytest.mark.parametrize("bad", ["AAPL;rm", "AA PL", "https://finance.yahoo.com/quote/AAPL", "ES=F:TOOLONGX",
                                 "ES=F:", "X" * 16, ",".join(f"S{i}" for i in range(21))])
def test_bad_entries(bad):
    with pytest.raises(ValueError):
        wl.parse_entries(bad)


def test_hand_edited_env_skips_bad_entries():
    assert wl.entries_from_env("AAPL,bad entry!,ES=F:S&P") == [("AAPL", "AAPL"), ("ES=F", "S&P")]
    assert len(wl.entries_from_env(",".join(f"S{i}" for i in range(30)))) == wl.MAX_SYMBOLS


def test_format_entries_roundtrip():
    entries = wl.parse_entries("AAPL,ES=F:S&P,CL=F:CL,^VIX")
    assert wl.format_entries(entries) == "AAPL,ES=F:S&P,CL=F,^VIX"
    assert wl.parse_entries(wl.format_entries(entries)) == entries


def test_packs_are_valid():
    for name, entries in wl.PACKS:
        parsed = wl.parse_entries(entries)
        assert parsed and len(parsed) <= wl.MAX_SYMBOLS, name
        assert all(len(label) <= 6 for _, label in parsed)
    assert ("ES=F", "S&P") in wl.parse_entries(wl.PACKS[0][1])


# --- quotes -------------------------------------------------------------------------
def test_equity_quote():
    q = wl.parse_quote(chart(190.0, 200.0), "AAPL", now=NOW)
    assert q["price"] == 190.0 and q["change"] == pytest.approx(-5.0) and q["points"] == -10.0
    assert q["currency"] == "usd" and not q["closed"] and q["intraday"] == [100, 101, 102, 101.5]


def test_future_has_no_currency_sign_and_closed_after_90_minutes():
    q = wl.parse_quote(chart(5900.0, 5880.0, kind="FUTURE", traded=NOW - 91 * 60), "ES=F", now=NOW)
    assert q["currency"] == "" and q["closed"]
    assert not wl.parse_quote(chart(5900.0, 5880.0, kind="FUTURE", traded=NOW - 80 * 60), "ES=F", now=NOW)["closed"]


@pytest.mark.parametrize("data", [
    NOT_FOUND, {}, {"chart": {"result": []}}, {"chart": {"result": [{"meta": "x"}]}}, "<html>", None,
    chart(None, 100.0), chart(float("nan"), 100.0), chart(100.0, 0), chart("12.5", 10.0)])
def test_malformed_data_raises(data):
    with pytest.raises(ValueError):
        wl.parse_quote(data, "AAPL", now=NOW)


def test_missing_closes_and_nulls():
    q = wl.parse_quote(chart(10.0, 9.0, closes=(None, 9.5, None, 10.0)), "X", now=NOW)
    assert q["intraday"] == [9.5, 10.0]
    data = chart(10.0, 9.0)
    data["chart"]["result"][0]["indicators"] = None
    assert wl.parse_quote(data, "X", now=NOW)["intraday"] == []


def test_mutual_fund_uses_the_month_chart():
    f = make("VTSAX", {("query2", "VTSAX"): (200, chart(150.0, 149.0, kind="MUTUALFUND", closes=(150.0,)))},
             month={"VTSAX": (200, chart(150.0, 140.0, closes=(140, 145, 148, 149, 150)))})
    (row,) = f.fetch()
    assert row["kind"] == "MUTUALFUND" and row["span"] == "1M" and row["intraday"] == [140, 145, 148, 149, 150]
    assert row["change"] == pytest.approx(100 / 149)       # the day's change, not the month's
    assert ("1mo", "VTSAX") in f.session.calls


def test_mutual_fund_without_month_chart_still_shows():
    f = make("VTSAX", {("query2", "VTSAX"): (200, chart(150.0, 149.0, kind="MUTUALFUND", closes=(150.0,)))})
    (row,) = f.fetch()
    assert row["price"] == 150.0 and "span" not in row


# --- the feed -----------------------------------------------------------------------
def test_one_bad_symbol_does_not_hide_the_others():
    f = make("AAPL,NOPE,ES=F:S&P", {("query2", "AAPL"): (200, chart(190.0, 200.0)),
                                     ("query2", "NOPE"): (200, NOT_FOUND),
                                     ("query2", "ES=F"): (200, chart(5900.0, 5880.0, kind="FUTURE"))})
    rows = f.fetch()
    assert [r["label"] for r in rows] == ["AAPL", "S&P"]
    st = f.status_for(f.entries)
    assert st["AAPL"]["ok"] and not st["NOPE"]["ok"] and "delisted" in st["NOPE"]["error"]


def test_rate_limited_host_falls_back():
    f = make("AAPL", {("query2", "AAPL"): (429, None), ("query1", "AAPL"): (200, chart(190.0, 200.0))})
    assert f.fetch()[0]["price"] == 190.0


def test_nothing_loads_raises_and_feed_keeps_running():
    f = make("AAPL", {}, default=429)
    with pytest.raises(ValueError):
        f.fetch()
    f._run()                                      # the background thread swallows it
    assert f.data is None


class Offline:
    headers = {}

    def get(self, *a, **k):
        raise feeds.requests.ConnectionError("Network is unreachable")


class Slow:
    headers = {}

    def get(self, *a, **k):
        raise feeds.requests.Timeout("read timed out")


@pytest.mark.parametrize("broken", [Offline, Slow])
def test_wifi_loss_keeps_the_last_good_quote_marked_stale(broken):
    f = make("AAPL,MSFT", {("query2", "AAPL"): (200, chart(190.0, 200.0)),
                           ("query2", "MSFT"): (200, chart(400.0, 390.0))})
    first = f.fetch()
    assert not any(r["stale"] for r in first)
    f.session = broken()
    rows = f.fetch()                              # nothing new, but nothing lost
    assert [r["price"] for r in rows] == [190.0, 400.0] and all(r["stale"] for r in rows)
    assert f.status_for(f.entries)["AAPL"]["had"]


def test_old_quotes_turn_stale():
    f = make("AAPL", {("query2", "AAPL"): (200, chart(190.0, 200.0))})
    f.fetch()
    assert not f.rows()[0]["stale"]
    assert f.rows(now=time.time() + 3 * 3600)[0]["stale"]


def test_everything_closed_slows_refresh():
    f = make("ES=F", {("query2", "ES=F"): (200, chart(5900.0, 5880.0, kind="FUTURE", traded=time.time() - 3 * 3600))})
    f.next_fetch = time.time() + 600
    f.fetch()
    assert f.next_fetch >= time.time() + wl.CLOSED_REFRESH - 5


def test_rotation():
    f = wl.Watchlist([])
    rows = list(range(7))
    assert f.next_batch(rows, 3) == [0, 1, 2] and f.next_batch(rows, 3) == [3, 4, 5]
    assert f.next_batch(rows, 3) == [6, 0, 1]
    assert f.next_batch(rows, 10) == rows and f.next_batch([], 3) == []


# --- the screen ---------------------------------------------------------------------
def quote_row(**kw):
    base = wl.parse_quote(chart(190.25, 200.0), "AAPL", now=NOW)
    base.update(label="AAPL", stale=False)
    base.update(kw)
    return base


def colors(img):
    return {img.getpixel((x, y)) for x in range(img.width) for y in range(img.height)}


def test_quote_screen():
    r = Renderer()
    img = r.quote(quote_row())
    assert img.size == (64, 32)
    assert SYMBOL in colors(img.crop((0, 0, 30, 8))) and DOWN in colors(img.crop((0, 9, 64, 16)))
    assert img.crop((0, 18, 64, 32)).getbbox()          # the day chart
    up = r.quote(quote_row(price=210.0, change=5.0, points=10.0))
    assert UP in colors(up.crop((0, 9, 64, 16)))


def test_currency_sign_only_for_money():
    r = Renderer()
    usd = r.quote(quote_row()).crop((20, 0, 64, 8))
    plain = r.quote(quote_row(currency="")).crop((20, 0, 64, 8))
    assert usd.tobytes() != plain.tobytes()


@pytest.mark.parametrize("kw,tag", [({"kind": "MUTUALFUND", "span": "1M"}, "NAV"), ({"closed": True}, "CLOSED")])
def test_tags(kw, tag):
    r = Renderer()
    plain = r.quote(quote_row()).crop((30, 9, 64, 16))
    tagged = r.quote(quote_row(**kw)).crop((30, 9, 64, 16))
    assert tagged.tobytes() != plain.tobytes()
    assert (150, 150, 150) in colors(tagged)            # dim tag, not a red/green number


def test_long_labels_and_prices_fit():
    r = Renderer()
    for label, price in (("PALLAD", 1234.5), ("NATGAS", 3.456), ("SILVER", 32.45), ("BTC-F", 109_235.0)):
        img = r.quote(quote_row(label=label, price=price, currency=""))
        assert img.getbbox()


def test_stale_mark():
    r = Renderer()
    assert r.quote(quote_row(), stale=True).getpixel((63, 31)) == DOWN


def test_list_page_uses_each_rows_currency():
    r = Renderer(currency="eur")
    rows = [{"symbol": "ES", "price": 5900.0, "change_24h": 1.0, "currency": ""},
            {"symbol": "AAPL", "price": 190.0, "change_24h": -1.0, "currency": "usd"}]
    a = r.list_page(rows, rows=4)
    b = r.list_page([dict(x, currency="eur") for x in rows], rows=4)
    assert a.tobytes() != b.tobytes()


# --- in the ticker ------------------------------------------------------------------
def make_ticker(monkeypatch, entries="AAPL,MSFT,ES=F:S&P", per_cycle=2, list_view=0):
    cfg = Config()
    cfg.screens = ["watchlist"]
    cfg.watchlist = wl.parse_entries(entries)
    cfg.watchlist_per_cycle = per_cycle
    cfg.list_view = list_view
    t = Ticker(cfg, FakeDisplay())
    t.watchlist.session = FakeSession({("query2", s): (200, chart(100.0 + i, 99.0)) for i, s in
                                       enumerate(("AAPL", "MSFT", "ES=F"))})
    monkeypatch.setattr(t.watchlist, "update", lambda now=None: t.watchlist.data)
    t.watchlist.data = t.watchlist.fetch()
    return t


def test_screens_rotate(monkeypatch):
    t = make_ticker(monkeypatch)
    first = t.screens()
    assert len(first) == 2 and all(s()[0].size == (64, 32) for s in first)
    assert len(t.screens()) == 2                        # the next two (MSFT... wraps around)
    assert t.watchlist._next == 4


def test_list_pages_follow(monkeypatch):
    t = make_ticker(monkeypatch, per_cycle=1, list_view=5)
    assert len(t.screens()) == 2                        # one chart, then one list page of all three


def test_screen_off_never_fetches():
    cfg = Config()
    cfg.screens = ["arcade"]
    cfg.watchlist = wl.parse_entries("AAPL")
    t = Ticker(cfg, FakeDisplay())
    t.watchlist.session = Offline()
    t.screens()
    assert t.watchlist.next_fetch == 0 and not t.watchlist.status


def test_empty_watchlist_shows_nothing():
    cfg = Config()
    cfg.screens = ["watchlist"]
    t = Ticker(cfg, FakeDisplay())
    assert t.screens() == []


def test_settings(monkeypatch):
    c = Config.from_env()
    assert "watchlist" not in c.screens and c.watchlist == [] and c.watchlist_refresh == 10
    monkeypatch.setenv("WATCHLIST", "aapl,ES=F:S&P,bad entry!")
    monkeypatch.setenv("WATCHLIST_REFRESH", "7")             # not a choice: default
    monkeypatch.setenv("WATCHLIST_PER_CYCLE", "50")
    c = Config.from_env()
    assert c.watchlist == [("AAPL", "AAPL"), ("ES=F", "S&P")] and c.watchlist_refresh == 10
    assert c.watchlist_per_cycle == 20
    monkeypatch.setenv("WATCHLIST_REFRESH", "30")
    assert Config.from_env().watchlist_refresh == 30


def test_settings_page(app):
    s = app.settings()
    assert "watchlist" in s["screen_names"] and s["watchlist"] == [] and s["watchlist_refresh"] == 10
    assert any(p["name"] == "Energy" for p in s["watchlist_packs"])
    saved = app.save({"watchlist": "aapl\nES=F:S&P\n^vix", "watchlist_refresh": "15", "watchlist_per_cycle": 4})
    assert saved == {"WATCHLIST": "AAPL,ES=F:S&P,^VIX", "WATCHLIST_REFRESH": "15", "WATCHLIST_PER_CYCLE": "4"}
    s = app.settings()
    assert s["watchlist"] == ["AAPL", "ES=F:S&P", "^VIX"] and s["watchlist_status"] == {
        "AAPL": None, "ES=F": None, "^VIX": None}
    for bad in ({"watchlist": "AAPL;ls"}, {"watchlist_refresh": "7"}, {"watchlist_per_cycle": 0},
                {"watchlist": ",".join(f"S{i}" for i in range(21))}):
        with pytest.raises(ValueError):
            app.save(bad)
    assert app.settings()["watchlist"] == ["AAPL", "ES=F:S&P", "^VIX"]   # nothing half-saved


def test_settings_page_bad_hand_edit(app):
    with open(app.env_path, "a") as fh:
        fh.write("WATCHLIST=AAPL;oops\n")
    assert app.settings()["watchlist"] == []
