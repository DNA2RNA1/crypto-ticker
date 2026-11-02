#!/usr/bin/env python3
"""Render the ticker to an animated GIF without any LED hardware.

    python preview.py                  # live prices from CoinGecko
    python preview.py --sample         # made-up prices, no price API needed
    python preview.py --rows 64        # preview a 64x64 panel
"""

import argparse
import logging
import os

from display import GifDisplay
from sample import apply_sample
from ticker import HERE, Config, Ticker, load_env_file


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sample", action="store_true", help="use made-up prices")
    p.add_argument("--rows", type=int)
    p.add_argument("--cols", type=int)
    p.add_argument("--out", default="preview.gif")
    p.add_argument("--scale", type=int, default=8)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    load_env_file(os.path.join(HERE, "settings.env"))
    cfg = Config.from_env()
    cfg.rows = args.rows or cfg.rows
    cfg.cols = args.cols or cfg.cols

    gif = GifDisplay(cfg.cols * cfg.chain, cfg.rows * cfg.parallel, scale=args.scale)
    ticker = Ticker(cfg, gif)
    ticker.loading()
    if args.sample:
        apply_sample(ticker)
    else:
        ticker.refresh(wait=True)  # live prices: fetch before recording
    ticker.step()
    if ticker.assets:  # slide back to the first coin so the GIF loops cleanly
        ticker.show(ticker.renderer.asset(ticker.assets[0]), 0.02)
        gif.frames.pop(), gif.durations.pop()
    gif.save(args.out)
    print(f"wrote {args.out} ({len(gif.frames)} frames)")


if __name__ == "__main__":
    main()
