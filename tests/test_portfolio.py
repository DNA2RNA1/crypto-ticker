"""Portfolio: holdings parsing, totals, the screen and the power-up reset."""

import pytest

from portfolio import format_holdings, parse_holdings, summarize
from render import DOWN, UP, Renderer


def test_parse_and_format_holdings():
    h = parse_holdings("BTC:0.5:30000, ada:12000 ,snek:1e5,bad, pepe:0")
    assert h == {"btc": (0.5, 30000.0), "ada": (12000.0, None)}  # bad/zero entries dropped
    assert format_holdings(h) == "btc:0.5:30000,ada:12000"
    with pytest.raises(ValueError):
        parse_holdings("btc:abc", strict=True)


def test_summary_day_change_cost_and_top():
    assets = [{"symbol": "BTC", "price": 100000.0, "change_24h": 10.0},
              {"symbol": "ADA", "price": 1.0, "change_24h": -50.0},
              {"symbol": "ETH", "price": 4000.0, "change_24h": 99.0}]   # not held: ignored
    p = summarize(assets, {"btc": (1.0, 50000.0), "ada": (1000.0, None)})
    assert p["value"] == pytest.approx(101000.0)
    prev = 100000 / 1.1 + 1000 / 0.5
    assert p["day_change"] == pytest.approx(101000 - prev)
    assert p["day_pct"] == pytest.approx((101000 - prev) / prev * 100)
    assert p["cost"] == 50000 and p["gain"] == pytest.approx(50000) and p["gain_pct"] == pytest.approx(100)
    assert p["top"] == ("BTC", 10.0)
    assert summarize(assets, {}) is None
    assert summarize(assets, {"ada": (5.0, None)})["gain_pct"] is None


def _texts(r, *args, **kw):
    drawn = []
    for name in ("4x6", "5x8", "6x10", "6x13B", "7x13B", "9x18B"):
        font = r.fonts[name]
        real = font.draw
        font.draw = (lambda real: lambda img, x, b, t, c: (drawn.append((x, t, font)), real(img, x, b, t, c))[1])(real)
    img = r.portfolio(*args, **kw)
    return img, [t for _, t, _ in drawn]


P = {"value": 123456.78, "day_change": 4210.5, "day_pct": 3.53, "cost": 96000.0,
     "gain": 27456.78, "gain_pct": 28.6, "top": ("SNEK", 12.4)}


def test_private_screen_has_no_dollars():
    for mode in ("24h", "total"):
        img, texts = _texts(Renderer(), P, True, mode)
        assert img.size == (64, 32) and not any("$" in t for t in texts)
    assert "+3.5%" in _texts(Renderer(), P, True, "24h")[1]
    assert "+28.6%" in _texts(Renderer(), P, True, "total")[1]
    no_cost = dict(P, gain=None, gain_pct=None, cost=None)
    assert "24H" in _texts(Renderer(), no_cost, True, "total")[1]  # falls back to 24h


def test_dollar_screen_shows_dollars_and_percent():
    img, texts = _texts(Renderer(), P, False, page="day")
    assert "$123,457" in texts and "+$4,210" in texts and "+3.5%" in texts
    img, texts = _texts(Renderer(), dict(P, day_change=-1890.0, day_pct=-1.5), False, page="day")
    colors = {img.getpixel((x, y)) for x in range(64) for y in range(32)}
    assert DOWN in colors and UP in colors  # red day change, green top performer


def make(portfolio=True, private=True, holdings=None):
    from sample import sample_assets
    from test_ticker import FakeDisplay
    from ticker import Config, Ticker
    cfg = Config()
    cfg.download_icons, cfg.screens = False, ["coins"]
    cfg.symbols = ["btc", "ada"]
    cfg.portfolio, cfg.portfolio_private = portfolio, private
    cfg.holdings = {"btc": (0.5, 30000.0)} if holdings is None else holdings
    t = Ticker(cfg, FakeDisplay())
    t.assets = sample_assets(["btc", "ada"])
    t.next_fetch = float("inf")
    return t


def test_portfolio_screens_in_rotation():
    assert make(portfolio=False).portfolio_screens() == []
    assert make(holdings={}).portfolio_screens() == []
    assert len(make(private=True).portfolio_screens()) == 1
    assert len(make(private=False).portfolio_screens()) == 2          # 24h page + since-cost page
    assert len(make(private=False, holdings={"btc": (1.0, None)}).portfolio_screens()) == 1
    for s in make(private=False).portfolio_screens():
        img, _ = s()
        assert img.size == (64, 32)


def test_power_up_resets_switches(tmp_path, monkeypatch):
    import sys
    import ticker
    env = tmp_path / "settings.env"
    env.write_text("PORTFOLIO=on\nPORTFOLIO_PRIVATE=off\nPORTFOLIO_HOLDINGS=btc:1\n")
    monkeypatch.setattr(sys, "argv", ["ticker.py", "--env", str(env), "--test-pattern"])
    monkeypatch.setattr(ticker, "setup_logging", lambda: None)
    import display as display_mod

    class Boom(Exception):
        pass

    def stop(*a, **k):
        raise Boom()
    monkeypatch.setattr(display_mod, "MatrixDisplay", stop)
    with pytest.raises(Boom):
        ticker.main()
    text = env.read_text()
    assert "PORTFOLIO=off" in text and "PORTFOLIO_PRIVATE=on" in text and "PORTFOLIO_HOLDINGS=btc:1" in text


def test_page_saves_holdings(tmp_path):
    from webui import WebApp
    env = tmp_path / "settings.env"
    env.write_text("WEB_PIN=1234\nSYMBOLS=btc,ada\n")
    app = WebApp(str(env))
    saved = app.save({"portfolio_holdings": "btc:0.5:30000,ada:1200", "portfolio": "on",
                      "portfolio_private": "off", "portfolio_pct": "total"})
    assert saved["PORTFOLIO_HOLDINGS"] == "btc:0.5:30000,ada:1200"
    s = app.settings()
    assert s["portfolio_holdings"] == {"btc": [0.5, 30000.0], "ada": [1200.0, None]}
    assert s["portfolio"] == "on" and s["portfolio_private"] == "off" and s["portfolio_pct"] == "total"
    with pytest.raises(ValueError):
        app.save({"portfolio_holdings": "btc:lots"})
