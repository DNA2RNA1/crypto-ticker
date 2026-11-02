#!/usr/bin/env python3
"""Render every ticker screen through RGBMatrixEmulator and save PNG screenshots.

    pip install -r requirements-dev.txt
    python screenshots.py                     # writes docs/screenshots/*.png
    python screenshots.py --out /tmp/shots --rows 64

Uses made-up sample data and a fixed date, so it needs no network or LED hardware
and the output is the same on every run.
"""

import argparse
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from display import MatrixDisplay
from prices import NetworkError, RateLimitError
from sample import apply_sample, sample_games, sample_news, sample_weather
from sports import BY_KEY
from ticker import HERE, Config, Ticker

FIXED_TIME = datetime(2026, 1, 15, 21, 41)
SPORTS_NOW = datetime(2026, 11, 7, 23, 0, tzinfo=timezone.utc)   # a Saturday evening in season
SPORTS_TZ = ZoneInfo("America/New_York")                          # only changes how times print


def build(rows, cols, layout="classic"):
    cfg = Config()
    cfg.symbols = ["btc:bitcoin", "eth:ethereum", "ada:cardano", "pepe:pepe", "snek:snek"]
    cfg.featured = ["snek"]
    cfg.rows, cfg.cols = rows, cols
    cfg.brightness = 100
    cfg.layout = layout
    cfg.download_icons = False   # offline: generated badges instead of downloaded logos
    cfg.weather_place = "Home"   # neutral label for the docs
    cfg.transition = "none"
    display = MatrixDisplay(cfg, emulate=True, adapter="raw", pixel_style="circle")
    ticker = Ticker(cfg, display)
    apply_sample(ticker)
    return ticker


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default=os.path.join(HERE, "docs", "screenshots"))
    p.add_argument("--rows", type=int, default=32)
    p.add_argument("--cols", type=int, default=64)
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING)
    os.makedirs(args.out, exist_ok=True)

    t = build(args.rows, args.cols)
    r = t.renderer
    by_sym = {a["symbol"]: a for a in t.assets}
    dow = t.yahoo_dow.data[0]
    index_rows = list(t.index_rows)
    index_rows.insert(1, {"label": "DOW", "change": dow["change"], "abs": dow["points"],
                          "unit": "", "spark": dow["intraday"]})

    from sample import sample_assets
    list5 = sample_assets(["btc", "eth", "sol", "ada", "pepe"])

    def rate_limited():
        t.last_error = RateLimitError("HTTP 429")
        return r.message("NO DATA", t.error_reason(), color=(230, 40, 30))

    def offline():
        t.last_error = NetworkError("no route to host")
        return r.message("NO DATA", t.error_reason(), color=(230, 40, 30))

    news_now = SPORTS_NOW.timestamp()
    news = sample_news(news_now)
    games = sample_games(SPORTS_NOW)

    def mid(anim):
        return anim.frame_at(anim.scroll_start + 0.45 * (anim.scroll_end - anim.scroll_start))

    def game(key, i, fav=None):
        g = dict(games[key][i], fav={"away": fav == "away", "home": fav == "home"})
        return r.game(g, BY_KEY[key], SPORTS_TZ, SPORTS_NOW.timestamp()).first_frame()

    def long_game():
        g = dict(games["ncaam"][0], fav={"away": True, "home": False})
        g["away"] = dict(g["away"], abbr="WASH", rank=25, score=105)
        return r.game(g, BY_KEY["ncaam"], SPORTS_TZ, SPORTS_NOW.timestamp())

    def chart(sym, stale=False):
        r.layout = "chart"
        try:
            return r.asset(by_sym[sym], stale=stale)
        finally:
            r.layout = "classic"

    shots = [
        ("01-loading", lambda: r.loading(2)),
        ("02-clock", lambda: r.clock(FIXED_TIME, t.weather.data, "F", t.cfg.weather_place)),
        ("03-fear-greed", lambda: r.fear_greed(t.fear_greed.data)),
        ("04-indices-percent", lambda: r.indices(index_rows, tag=None)),
        ("05-indices-change", lambda: r.indices(index_rows, tag=None, mode="abs")),
        ("06-featured", lambda: r.featured(by_sym["SNEK"])),
        ("07-coin-classic", lambda: r.asset(by_sym["BTC"])),
        ("08-coin-chart", lambda: chart("ETH")),
        ("09-coin-tiny-price", lambda: r.asset(by_sym["PEPE"])),
        ("10-coin-stale", lambda: r.asset(by_sym["ADA"], stale=True)),
        ("10b-coin-chart-stale", lambda: chart("ADA", stale=True)),
        ("10c-list-view-5", lambda: r.list_page(list5, rows=5)),
        ("10d-list-view-4", lambda: r.list_page(list5[:4], rows=4)),
        ("10e-list-view-5-stale", lambda: r.list_page(list5, rows=5, stale=True)),
        ("11-no-data-offline", offline),
        ("12-no-data-rate-limit", rate_limited),
        ("13-weather", lambda: r.weather(sample_weather(), "F", FIXED_TIME.replace(hour=14))),
        ("13b-weather-rain", lambda: r.weather(sample_weather(61), "F", FIXED_TIME.replace(hour=14))),
        ("14-news", lambda: r.news(news[0], now_ts=news_now).first_frame()),
        ("14b-news-scrolling", lambda: mid(r.news(news[0], now_ts=news_now))),
        ("15-sports-live", lambda: game("nfl", 0, fav="away")),
        ("16-sports-college-ranked", lambda: game("ncaaf", 0)),
        ("17-sports-final", lambda: game("nba", 1)),
        ("18-sports-upcoming", lambda: game("mlb", 2, fav="home")),
        ("19-sports-row-scrolling", lambda: mid(long_game())),
    ]
    paths = []
    for name, make in shots:
        t.display.show(make())
        path = t.display.screenshot(os.path.join(args.out, f"{name}.png"))
        paths.append((name, path))
        print("wrote", os.path.relpath(path))
    sheet = contact_sheet(paths, os.path.join(args.out, "panel-contact-sheet.png"))
    print("wrote", os.path.relpath(sheet))
    gif = transition_gif(os.path.join(HERE, "docs", "preview-scroll.gif"), args.rows, args.cols)
    print("wrote", os.path.relpath(gif))
    for gif in (news_gif(os.path.join(HERE, "docs", "preview-news.gif")),
                sports_gif(os.path.join(HERE, "docs", "preview-sports.gif"))):
        print("wrote", os.path.relpath(gif))


def _gif_ticker(transition="scroll"):
    from display import GifDisplay
    cfg = Config()
    cfg.download_icons = False
    cfg.transition = transition
    gif = GifDisplay(64, 32, scale=4)
    return Ticker(cfg, gif), gif


def news_gif(out):
    """Two headlines scrolling, made by the real Ticker.play() code (one pixel per frame)."""
    t, gif = _gif_ticker()
    now = SPORTS_NOW.timestamp()
    items = sample_news(now)
    for item in items[:2]:
        t.play(t.renderer.news(item, now_ts=now))
    t.play(t.renderer.news(items[0], now_ts=now))   # back to the start so it loops
    gif.save(out)
    return out


def sports_gif(out):
    """A few games with the scroll transition; one row is too long and scrolls."""
    t, gif = _gif_ticker()
    games = sample_games(SPORTS_NOW)
    ts = SPORTS_NOW.timestamp()
    g = dict(games["ncaam"][0], fav={"away": True, "home": False})
    g["away"] = dict(g["away"], abbr="WASH", rank=25, score=105)
    seq = [(dict(games["nfl"][0], fav={"away": True, "home": False}), "nfl"), (g, "ncaam"),
           (dict(games["nba"][1], fav={}), "nba"), (dict(games["nfl"][0], fav={"away": True, "home": False}), "nfl")]
    for game, key in seq:
        t.play(t.renderer.game(game, BY_KEY[key], SPORTS_TZ, ts, hold=2.0))
    gif.save(out)
    return out


def transition_gif(out, rows=32, cols=64, transition="scroll", hold=1.2):
    """Animated GIF of the screen transition, made by the real Ticker.show() code."""
    from display import GifDisplay
    from sample import sample_assets
    cfg = Config()
    cfg.rows, cfg.cols = rows, cols
    cfg.download_icons = False
    cfg.transition = transition
    cfg.weather_place = "Home"
    gif = GifDisplay(cols, rows, scale=6)
    t = Ticker(cfg, gif)
    apply_sample(t)
    r = t.renderer
    listed = sample_assets(["btc", "eth", "sol", "ada", "doge", "link", "hbar", "pepe"])
    coins = {a["symbol"]: a for a in listed}
    r.layout = "classic"
    first = r.asset(coins["BTC"])
    r.layout = "chart"
    screens = [first, r.asset(coins["ETH"]),
               r.list_page(listed[:4], rows=4), r.list_page(listed[4:], rows=4),  # page 1 -> page 2
               r.clock(FIXED_TIME, t.weather.data, "F", "Home")]
    r.layout = "classic"
    for img in screens:
        t.show(img, hold)
    t.show(first, 0.02)          # scroll back to the start so the loop is seamless
    gif.frames.pop(), gif.durations.pop()
    gif.save(out)
    return out


LABELS = {
    "01-loading": "Loading", "02-clock": "Clock + weather", "03-fear-greed": "Fear & Greed",
    "04-indices-percent": "Indices (% change)", "05-indices-change": "Indices ($ / points, optional)",
    "06-featured": "Featured (starred) coin", "07-coin-classic": "Coin: classic layout",
    "08-coin-chart": "Coin: chart layout", "09-coin-tiny-price": "Coin: tiny price",
    "10-coin-stale": "Stale price (red dot)", "10b-coin-chart-stale": "Stale price, chart layout",
    "10c-list-view-5": "List view: 5 rows", "10d-list-view-4": "List view: 4 rows + %",
    "10e-list-view-5-stale": "List view, stale", "11-no-data-offline": "Offline, no cached prices",
    "12-no-data-rate-limit": "Rate limited, no cached prices",
    "13-weather": "Weather: temp, high/low, next 12 h", "13b-weather-rain": "Weather: rain",
    "14-news": "News headline (tag + age)", "14b-news-scrolling": "News: scrolling",
    "15-sports-live": "Sports: live, favorite", "16-sports-college-ranked": "Sports: college, ranked",
    "17-sports-final": "Sports: final (OT)", "18-sports-upcoming": "Sports: upcoming",
    "19-sports-row-scrolling": "Sports: long row scrolls",
}


def contact_sheet(paths, out, cols=4, scale=0.5):
    """All panel screenshots in one labeled grid."""
    from PIL import Image, ImageDraw, ImageFont
    shots = [(LABELS.get(n, n), Image.open(p).convert("RGB")) for n, p in paths]
    w = int(shots[0][1].width * scale)
    h = int(shots[0][1].height * scale)
    pad, label_h, title_h = 16, 30, 56
    rows = (len(shots) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (w + pad) + pad, title_h + rows * (h + label_h + pad) + pad), (24, 26, 30))
    d = ImageDraw.Draw(sheet)
    try:
        title_font, font = ImageFont.load_default(26), ImageFont.load_default(17)
    except TypeError:  # Pillow < 10.1
        title_font = font = ImageFont.load_default()
    d.text((pad, 16), "Crypto Ticker: every panel screen (64x32, emulator, sample data)", fill=(235, 238, 242),
           font=title_font)
    for i, (label, img) in enumerate(shots):
        x = pad + (i % cols) * (w + pad)
        y = title_h + (i // cols) * (h + label_h + pad)
        sheet.paste(img.resize((w, h), Image.LANCZOS), (x, y))
        d.text((x, y + h + 6), label, fill=(170, 176, 186), font=font)
    sheet.save(out)
    return out


if __name__ == "__main__":
    main()
