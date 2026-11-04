"""Focus mode and List view: which coins rotate, and how list pages are drawn."""

import pytest

from render import DOWN, UP, Renderer
from sample import sample_assets
from test_ticker import FakeDisplay

from ticker import Config, Ticker

SYMS = ["btc", "eth", "ada", "sol", "pepe", "doge", "link"]


def ticker(focus=False, featured=(), list_view=0):
    cfg = Config()
    cfg.symbols = SYMS
    cfg.screens = ["coins"]
    cfg.download_icons = False
    cfg.focus, cfg.featured, cfg.list_view = focus, list(featured), list_view
    t = Ticker(cfg, FakeDisplay())
    t.assets = sample_assets(SYMS)
    t.next_fetch = float("inf")
    return t


def kinds(t, monkeypatch):
    """Which renderer calls the coin rotation makes, in order."""
    calls = []
    r = t.renderer
    monkeypatch.setattr(r, "featured", lambda a, stale=False: calls.append(("big", a["symbol"])))
    monkeypatch.setattr(r, "asset", lambda a, stale=False, stamp=None: calls.append(("coin", a["symbol"])))
    monkeypatch.setattr(r, "list_page", lambda page, rows=5, stale=False:
                        calls.append(("list", tuple(a["symbol"] for a in page))))
    for screen in t.coin_screens():
        screen()
    return calls


def test_focus_off_shows_everything(monkeypatch):
    calls = kinds(ticker(featured=["ada"]), monkeypatch)
    assert [c[1] for c in calls if c[0] == "coin"] == [s.upper() for s in SYMS]
    assert ("big", "ADA") in calls


def test_focus_on_shows_only_starred(monkeypatch):
    calls = kinds(ticker(focus=True, featured=["sol", "btc"]), monkeypatch)
    assert calls == [("big", "BTC"), ("big", "SOL")]  # big screen only, no doubling


def test_focus_big_screen_gets_full_time():
    t = ticker(focus=True, featured=["btc"])
    t.cfg.sleep = 7
    holds = [s()[1] for s in t.coin_screens()]
    assert holds == [7]


def test_starred_outside_focus_still_get_both_screens(monkeypatch):
    calls = kinds(ticker(featured=["btc"]), monkeypatch)
    assert calls[:2] == [("big", "BTC"), ("coin", "BTC")]


def test_focus_on_with_nothing_starred_shows_all(monkeypatch):
    calls = kinds(ticker(focus=True, featured=["xyz"]), monkeypatch)
    assert len([c for c in calls if c[0] == "coin"]) == len(SYMS)


def test_focus_keeps_other_screens():
    t = ticker(focus=True, featured=["btc"])
    t.cfg.screens = ["clock", "coins"]
    t.weather.next_fetch = float("inf")
    assert len(t.screens()) == 1 + 1  # clock + BTC's big screen


@pytest.mark.parametrize("n,pages", [(5, [("BTC", "ETH", "ADA", "SOL", "PEPE"), ("DOGE", "LINK")]),
                                     (4, [("BTC", "ETH", "ADA", "SOL"), ("PEPE", "DOGE", "LINK")])])
def test_list_view_pages(monkeypatch, n, pages):
    calls = kinds(ticker(list_view=n, featured=["eth"]), monkeypatch)
    # every coin full screen first (ETH starred: big + regular), then the list pages last
    singles = [c[1] for c in calls if c[0] == "coin"]
    assert singles == [s.upper() for s in SYMS]
    assert ("big", "ETH") in calls
    first_list = next(i for i, c in enumerate(calls) if c[0] == "list")
    assert all(c[0] == "list" for c in calls[first_list:])
    assert [c[1] for c in calls[first_list:]] == pages


def test_list_view_with_focus_lists_only_starred(monkeypatch):
    calls = kinds(ticker(list_view=5, focus=True, featured=["doge", "btc"]), monkeypatch)
    assert calls == [("big", "BTC"), ("big", "DOGE"), ("list", ("BTC", "DOGE"))]


def test_list_page_seconds_used():
    t = ticker(list_view=5)
    t.cfg.list_page_seconds = 9
    t.cfg.sleep = 4
    holds = [s()[1] for s in t.coin_screens()]
    assert holds == [4] * len(SYMS) + [9, 9]  # coins at sleep, then 2 list pages


@pytest.mark.parametrize("rows", [5, 4])
def test_list_page_draws_rows_in_trend_colors(rows):
    r = Renderer()
    assets = sample_assets(SYMS)[:rows]
    for a, ch in zip(assets, [5, -5, 5, -5, 5]):
        a["change_24h"] = ch
    img = r.list_page(assets, rows=rows)
    assert img.size == (64, 32)
    colors = {img.getpixel((x, y)) for x in range(64) for y in range(32)}
    assert UP in colors and DOWN in colors
    line = 32 // rows
    for i in range(rows):  # every row has lit pixels, and nothing runs off the right edge
        band = img.crop((0, i * line, 64, (i + 1) * line))
        assert band.getbbox() is not None


def test_list_page_fits_extreme_values():
    r = Renderer(currency="eur")
    a = sample_assets(["btc", "pepe", "x", "y"])
    a[0]["price"], a[0]["change_24h"] = 1234567.89, -123.4
    a[1]["price"] = 0.0000000123
    a[2]["change_24h"] = None
    a[3]["symbol"] = "LONGSYM"
    for rows in (5, 4):
        r.list_page(a, rows=rows, stale=True)


def test_list_settings_from_env(monkeypatch):
    monkeypatch.setenv("LIST_VIEW", "4")
    monkeypatch.setenv("FOCUS", "on")
    monkeypatch.setenv("LIST_PAGE_SECONDS", "120")
    c = Config.from_env()
    assert c.list_view == 4 and c.focus is True and c.list_page_seconds == 60
    monkeypatch.setenv("LIST_VIEW", "6")
    assert Config.from_env().list_view == 0


def test_5x8_punctuation_sits_on_the_baseline():
    """'.' and ',' must not drop into the descender row (they looked like '+' marks)."""
    from PIL import Image

    from render import load_fonts

    font = load_fonts()["5x8"]
    for ch in ".,":
        img = Image.new("1", (5, 12), 0)
        font.draw(img, 0, 9, ch, 1)          # baseline at y=9: glyph rows must end at y=8
        rows = [y for y in range(12) if any(img.getpixel((x, y)) for x in range(5))]
        digit = Image.new("1", (5, 12), 0)
        font.draw(digit, 0, 9, "3", 1)
        digit_rows = [y for y in range(12) if any(digit.getpixel((x, y)) for x in range(5))]
        assert max(rows) == max(digit_rows), ch     # same bottom row as the digits
        assert len(rows) <= 3, ch                   # a dot / short comma, not a plus


@pytest.mark.parametrize("currency", ["usd", "eur", "jpy"])
@pytest.mark.parametrize("rows", [4, 5])
def test_list_rows_fit_without_percent(currency, rows):
    r = Renderer(currency=currency)
    font = r.fonts["5x8"] if rows == 4 else r.fonts["4x6"]
    drawn = []
    real = font.draw

    def spy(img, x, baseline, text, color):
        drawn.append((x, baseline, text))
        return real(img, x, baseline, text, color)

    font.draw = spy
    for price in (109234.56, 4012.37, 999.99, 212.44, 22.41, 0.8123, 0.00001123, 1234567.0):
        for change in (-2.91, 12.345, -123.4, 0.0, None):
            for sym in ("ETH", "PEPE", "LONGSYM"):
                a = sample_assets(["btc"])[0] | {"symbol": sym, "price": price, "change_24h": change}
                drawn.clear()
                r.list_page([a], rows=rows)
                assert len(drawn) == 2 and not any("%" in t for _, _, t in drawn), drawn
                for x, _, text in drawn:                 # nothing overlaps or runs off the edge
                    assert x >= 0 and x + font.width(text) <= 64, (price, change, sym, drawn)
                (x1, _, t1), (x2, _, _) = sorted(drawn)
                assert x1 + font.width(t1) <= x2, drawn


def test_eth_example_keeps_the_whole_price():
    r = Renderer()
    a = sample_assets(["eth"])[0] | {"price": 4012.37, "change_24h": -2.91}
    font = r.fonts["5x8"]
    drawn = []
    real = font.draw
    font.draw = lambda img, x, b, t, c: (drawn.append(t), real(img, x, b, t, c))[1]
    r.list_page([a], rows=4)
    assert drawn == ["ETH", "$4,012"]


@pytest.mark.parametrize("price", [0.00009999, 0.000099999, 0.0000999, 0.00001123, 0.0000000123])
def test_tiny_prices_render_everywhere(price):
    from render import price_candidates
    assert all(isinstance(c, str) for c in price_candidates(price, "usd"))
    r = Renderer()
    a = sample_assets(["pepe"])[0] | {"price": price}
    for layout in ("classic", "chart"):
        r.layout = layout
        r.asset(a)
    r.featured(a)
    for rows in (4, 5):
        r.list_page([a], rows=rows)


@pytest.mark.parametrize("name", ["5x8", "6x10", "6x12", "7x13", "6x13B", "7x13B", "9x18B"])
def test_punctuation_on_the_baseline_in_every_redrawn_font(name):
    from PIL import Image

    from render import load_fonts

    font = load_fonts()[name]
    size = font.ascent + font.descent + 4

    def rows(text):
        img = Image.new("1", (font.width(text), size), 0)
        font.draw(img, 0, font.ascent + 2, text, 1)
        return [y for y in range(size) if any(img.getpixel((x, y)) for x in range(img.width))]

    bottom = max(rows("0123"))
    for ch in ".,":
        assert max(rows(ch)) == bottom, (name, ch)        # nothing hangs below the digits


def make_index_ticker(view="percent", source="coingecko", style="compact"):
    from sample import apply_sample
    from test_ticker import FakeDisplay
    from ticker import Config, Ticker

    cfg = Config()
    cfg.download_icons = False
    cfg.screens = ["indices"]
    cfg.indices_view, cfg.indices_source, cfg.sleep = view, source, 8
    cfg.indices_style = style
    t = Ticker(cfg, FakeDisplay())
    apply_sample(t)
    return t


def test_coingecko_index_rows_are_labeled_in_dollars():
    """Tokenized-fund moves are dollars per share, not index points: keep the "$"."""
    t = make_index_ticker()
    assert all(r["unit"] == "$" for r in t.index_rows)
    assert Renderer.abs_text(3.24, "$") == "+$3.24"
    assert Renderer.abs_text(81.4, "") == "+81.4"       # the Dow (Yahoo) is in points


def test_indices_show_percent_only_by_default():
    t = make_index_ticker()
    screens = [s() for s in t.screens()]
    assert len(screens) == 1 and screens[0][1] == 8    # one % screen for the full time


def test_indices_both_flips_to_dollar_and_point_change():
    t = make_index_ticker(view="both")
    screens = [s() for s in t.screens()]
    assert [s[1] for s in screens] == [4.0, 4.0] and screens[1][2] is False  # flip in place


def test_indices_view_setting(monkeypatch):
    from ticker import Config
    assert Config.from_env().indices_view == "percent"
    monkeypatch.setenv("INDICES_VIEW", "both")
    assert Config.from_env().indices_view == "both"
    monkeypatch.setenv("INDICES_VIEW", "points")
    assert Config.from_env().indices_view == "percent"


@pytest.mark.parametrize("rows", [5, 4])
@pytest.mark.parametrize("transition,frames", [("scroll", 13), ("slide", 13), ("none", 0)])
def test_list_pages_use_the_transition_setting(rows, transition, frames):
    """Each list page rolls into the next with the chosen transition and the usual timing."""
    from ticker import transition_frames

    t = ticker(list_view=rows)                 # 7 coins -> 2 pages, nothing starred
    t.cfg.transition = transition
    t.cfg.list_page_seconds = 6
    shown = []
    t.display.show = lambda img, hold=0: shown.append((img.copy(), hold))
    t.step()
    pages = [i for i, (_, hold) in enumerate(shown) if hold == 6]
    assert len(pages) == 2
    between = shown[pages[0] + 1:pages[1]]     # frames between page 1 and page 2
    assert len(between) == frames
    assert sum(h for _, h in between) == pytest.approx(0.45 * frames / 14)
    if frames:
        expected = transition_frames(shown[pages[0]][0], shown[pages[1]][0], vertical=transition == "scroll")
        assert all(a.tobytes() == b.tobytes() for (a, _), b in zip(between, expected))


def test_list_row_shows_whole_price_without_percent():
    r = Renderer()
    a = sample_assets(["btc"])
    a[0]["price"], a[0]["change_24h"] = 77530.0, 2.0
    for rows in (5, 4):
        img = r.list_page(a, rows=rows)
        font = r.fonts["4x6"] if rows == 5 else r.fonts["5x8"]
        # the full "$77,530" fits next to BTC, so the K form is never chosen
        assert font.width("BTC") + 3 + font.width("$77,530") <= 64
        colors = {img.getpixel((x, y)) for x in range(64) for y in range(32)}
        assert UP in colors and DOWN not in colors


def test_coins_screen_can_be_turned_off():
    import webui
    assert webui._screens("clock,weather") == "clock,weather"
    assert webui._screens("coins,clock") == "clock,coins"  # coins stay last
    assert webui._screens("") == "coins"                     # never an empty rotation


def test_no_coingecko_calls_when_crypto_prices_are_off():
    t = ticker()
    t.cfg.screens = ["clock"]
    t.next_fetch = 0
    t.refresh(wait=True)
    assert t._fetching is None and t.last_success in (0, None, 0.0)


def test_coingecko_indices_still_fetch_with_coins_off():
    t = ticker()
    t.cfg.screens = ["indices"]
    t.cfg.indices_source = "coingecko"
    assert t._needs_prices(t.cfg)
    t.cfg.indices_source = "yahoo"
    assert not t._needs_prices(t.cfg)


def test_index_charts_one_screen_per_index():
    t = make_index_ticker(style="charts")
    shots = [s() for s in t.screens()]
    assert len(shots) == 3 and all(h == 8 for _, h in shots)   # S&P, DOW, NAS
    for img, _ in shots:
        assert img.size == (64, 32)


def test_index_chart_shows_level_and_reference_line():
    from render import DOWN, UP, Renderer
    r = Renderer()
    pts = [100.0, 101.0, 99.0, 98.5, 100.5, 101.5]          # crosses yesterday's close (100)
    img = r.index_chart({"label": "DOW", "change": 1.5, "abs": 1.5, "unit": "",
                         "spark": pts, "price": 51983.98, "prev_close": 100.0})
    colors = {img.getpixel((x, y)) for x in range(64) for y in range(18, 32)}
    assert UP in colors and DOWN in colors                  # green above, red below the close
    assert (120, 120, 130) in colors                        # dotted close line


def test_index_chart_without_level_or_close():
    from render import Renderer
    img = Renderer().index_chart({"label": "S&P", "change": -0.4, "abs": -3.1, "unit": "$",
                                  "spark": [5, 4.9, 4.8]})
    assert img.size == (64, 32)


def test_indices_style_setting(monkeypatch):
    from ticker import Config
    assert Config.from_env().indices_style == "charts"
    monkeypatch.setenv("INDICES_STYLE", "compact")
    assert Config.from_env().indices_style == "compact"


def test_stale_prices_get_a_dated_notice_first(monkeypatch):
    import time as _time
    from prices import NetworkError
    t = ticker()
    t.stale, t.stale_since, t.last_error = True, _time.time() - 3600, NetworkError("down")
    seen = []
    monkeypatch.setattr(t.renderer, "stale_notice", lambda when, reason: seen.append((when, reason)))
    monkeypatch.setattr(t.renderer, "asset", lambda a, stale=False: None)
    screens = t.coin_screens()
    screens[0]()
    assert seen and seen[0][1] == "NO NETWORK?" and len(screens) == len(SYMS) + 1


def test_live_prices_have_no_notice():
    t = ticker()
    t.stale = False
    assert len(t.coin_screens()) == len(SYMS)


def test_stale_notice_fits():
    from datetime import datetime
    from render import Renderer
    r = Renderer()
    font = r.fonts["5x8"]
    drawn = []
    real = font.draw
    font.draw = lambda img, x, b, txt, c: (drawn.append((x, txt)), real(img, x, b, txt, c))[1]
    img = r.stale_notice(datetime(2026, 11, 12, 22, 41), "NO NETWORK?")
    assert img.size == (64, 32) and drawn[-1][1] == "11/12 10:41P"
    assert all(x >= 0 and x + font.width(txt) <= 64 for x, txt in drawn)


def test_coin_screen_shows_fetch_time(monkeypatch):
    import time as _time
    t = ticker()
    t.stale, t.last_success = False, _time.time()
    seen = []
    monkeypatch.setattr(t.renderer, "asset", lambda a, stale=False, stamp=None: seen.append(stamp))
    for s in t.coin_screens():
        s()
    assert seen and all(st is not None for st in seen)
    t.last_success = 0
    assert t.price_time() is None


def test_tiny_time_fits_under_the_chart():
    from datetime import datetime
    from render import Renderer, tiny_width
    from sample import sample_assets
    r = Renderer()
    r.layout = "classic"
    a = sample_assets(["ada"])[0] | {"change_24h": -2.7}
    img = r.asset(a, stamp=datetime(2026, 11, 4, 22, 41))
    assert img.size == (64, 32) and tiny_width("22:41") == 17
    rows = {y for y in range(32) for x in range(64) if img.getpixel((x, y)) == (110, 110, 120)}
    assert rows and rows <= set(range(15, 20))  # drawn in rows 15-19 only
    # a long symbol with a big change leaves no room: the time is simply left out
    b = sample_assets(["night"])[0] | {"change_24h": -12.5}
    assert r.asset(b, stamp=datetime(2026, 11, 4, 22, 41)).size == (64, 32)
