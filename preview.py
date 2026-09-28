#!/usr/bin/env python3
"""Render the ticker to an animated GIF without any LED hardware.

    python preview.py                  # live prices from CoinGecko
    python preview.py --sample         # made-up prices, no price API needed
    python preview.py --rows 64        # preview a 64x64 panel
"""

import argparse
import logging
import math
import os
import random

from display import GifDisplay
from ticker import HERE, Config, Ticker, load_env_file


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
        ticker.assets = sample_assets(cfg.symbols)
        ticker.next_fetch = float("inf")
    ticker.step()
    if ticker.assets:  # slide back to the first coin so the GIF loops cleanly
        ticker.show(ticker.renderer.asset(ticker.assets[0]), 0.02)
        gif.frames.pop(), gif.durations.pop()
    gif.save(args.out)
    print(f"wrote {args.out} ({len(gif.frames)} frames)")


if __name__ == "__main__":
    main()
