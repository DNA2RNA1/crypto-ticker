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
    monkeypatch.setattr(r, "asset", lambda a, stale=False: calls.append(("coin", a["symbol"])))
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
    assert calls == [("big", "BTC"), ("coin", "BTC"), ("big", "SOL"), ("coin", "SOL")]


def test_focus_on_with_nothing_starred_shows_all(monkeypatch):
    calls = kinds(ticker(focus=True, featured=["xyz"]), monkeypatch)
    assert len([c for c in calls if c[0] == "coin"]) == len(SYMS)


def test_focus_keeps_other_screens():
    t = ticker(focus=True, featured=["btc"])
    t.cfg.screens = ["clock", "coins"]
    t.weather.next_fetch = float("inf")
    assert len(t.screens()) == 1 + 2  # clock + BTC's big screen + BTC


@pytest.mark.parametrize("n,pages", [(5, [("BTC", "ETH", "ADA", "SOL", "PEPE"), ("DOGE", "LINK")]),
                                     (4, [("BTC", "ETH", "ADA", "SOL"), ("PEPE", "DOGE", "LINK")])])
def test_list_view_pages(monkeypatch, n, pages):
    calls = kinds(ticker(list_view=n, featured=["eth"]), monkeypatch)
    assert calls[:2] == [("big", "ETH"), ("coin", "ETH")]  # starred keep big screens
    assert [c[1] for c in calls if c[0] == "list"] == pages


def test_list_view_with_focus_lists_only_starred(monkeypatch):
    calls = kinds(ticker(list_view=5, focus=True, featured=["doge", "btc"]), monkeypatch)
    assert [c for c in calls if c[0] == "list"] == [("list", ("BTC", "DOGE"))]


def test_list_page_seconds_used():
    t = ticker(list_view=5)
    t.cfg.list_page_seconds = 9
    holds = [s()[1] for s in t.coin_screens()]
    assert holds == [9, 9]


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
def test_4_row_list_always_shows_percent_and_fits(currency):
    r = Renderer(currency=currency)
    font = r.fonts["5x8"]
    drawn = []
    real = font.draw

    def spy(img, x, baseline, text, color):
        drawn.append((x, baseline, text))
        return real(img, x, baseline, text, color)

    font.draw = spy
    for price in (109234.56, 4012.37, 999.99, 212.44, 22.41, 0.8123, 0.00001123, 1234567.0):
        for change in (-2.91, 12.345, -123.4, 0.0):
            for sym in ("ETH", "PEPE", "LONGSYM"):
                a = sample_assets(["btc"])[0] | {"symbol": sym, "price": price, "change_24h": change}
                drawn.clear()
                r.list_page([a], rows=4)
                ctext = drawn[-1][2]
                assert ctext.endswith("%"), (price, change, sym, drawn)
                for x, _, text in drawn:                 # nothing overlaps or runs off the edge
                    assert x >= 0 and x + font.width(text) <= 64, (price, change, sym, drawn)
                xs = sorted((x, x + font.width(t)) for x, _, t in drawn)
                assert all(a_end <= b_start for (_, a_end), (b_start, _) in zip(xs, xs[1:])), drawn


def test_eth_example_uses_short_price_not_dropped_percent():
    r = Renderer()
    a = sample_assets(["eth"])[0] | {"price": 4012.37, "change_24h": -2.91}
    font = r.fonts["5x8"]
    drawn = []
    real = font.draw
    font.draw = lambda img, x, b, t, c: (drawn.append(t), real(img, x, b, t, c))[1]
    r.list_page([a], rows=4)
    assert drawn == ["ETH", "4.0K", "▼2.9%"]


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


def make_index_ticker(view="percent", source="coingecko"):
    from sample import apply_sample
    from test_ticker import FakeDisplay
    from ticker import Config, Ticker

    cfg = Config()
    cfg.download_icons = False
    cfg.screens = ["indices"]
    cfg.indices_view, cfg.indices_source, cfg.sleep = view, source, 8
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
