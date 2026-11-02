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
    ticker.yahoo_dow.data = [{"label": "DOW", "change": 0.18, "points": 81.4, "intraday": up}]
    ticker.fear_greed.data = {"value": 70, "label": "Greed", "yesterday": 65,
                              "history": [60, 58, 62, 66, 64, 63, 65, 70]}
    ticker.weather.data = {"temp": 76.8, "code": 0, "is_day": False}
