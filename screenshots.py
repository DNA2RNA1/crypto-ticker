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
from datetime import datetime

from display import MatrixDisplay
from prices import NetworkError, RateLimitError
from sample import apply_sample
from ticker import HERE, Config, Ticker

FIXED_TIME = datetime(2026, 1, 15, 21, 41)


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
    ]
    paths = []
    for name, make in shots:
        t.display.show(make())
        path = t.display.screenshot(os.path.join(args.out, f"{name}.png"))
        paths.append((name, path))
        print("wrote", os.path.relpath(path))
    sheet = contact_sheet(paths, os.path.join(args.out, "panel-contact-sheet.png"))
    print("wrote", os.path.relpath(sheet))


LABELS = {
    "01-loading": "Loading", "02-clock": "Clock + weather", "03-fear-greed": "Fear & Greed",
    "04-indices-percent": "Indices (% change)", "05-indices-change": "Indices ($ / points, optional)",
    "06-featured": "Featured (starred) coin", "07-coin-classic": "Coin: classic layout",
    "08-coin-chart": "Coin: chart layout", "09-coin-tiny-price": "Coin: tiny price",
    "10-coin-stale": "Stale price (red dot)", "10b-coin-chart-stale": "Stale price, chart layout",
    "10c-list-view-5": "List view: 5 rows", "10d-list-view-4": "List view: 4 rows + %",
    "10e-list-view-5-stale": "List view, stale", "11-no-data-offline": "Offline, no cached prices",
    "12-no-data-rate-limit": "Rate limited, no cached prices",
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
