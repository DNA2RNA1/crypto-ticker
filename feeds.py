"""Extra data for non-coin screens: Fear & Greed index and stock market indices.

Neither uses CoinGecko, so they don't count against the monthly CoinGecko budget.
Each keeps its last good data; if a source fails, its screen is skipped (never a crash).
"""

import logging
import threading
import time

import requests

log = logging.getLogger("ticker.feeds")

BROWSER_UA = ("Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")


class Feed:
    refresh_every = 300

    def __init__(self):
        self.data = None
        self.next_fetch = 0.0
        self._busy = False
        self.session = requests.Session()
        self.session.headers["User-Agent"] = BROWSER_UA

    def update(self, now=None):
        """Return the latest data; start a background refresh if one is due."""
        now = now or time.time()
        if now >= self.next_fetch and not self._busy:
            self._busy = True
            self.next_fetch = now + self.refresh_every
            threading.Thread(target=self._run, daemon=True).start()
        return self.data

    def _run(self):
        try:
            self.data = self.fetch()
        except Exception as exc:  # any failure: keep old data, retry in a few minutes
            log.warning("%s fetch failed: %s", type(self).__name__, exc)
            self.next_fetch = time.time() + min(300, self.refresh_every)
        finally:
            self._busy = False

    def fetch(self):
        raise NotImplementedError


class Weather(Feed):
    """Outdoor temperature and conditions from Open-Meteo (free, no key, non-commercial)."""
    URL = "https://api.open-meteo.com/v1/forecast"
    refresh_every = 900

    def __init__(self, lat, lon, unit="fahrenheit"):
        super().__init__()
        self.lat, self.lon, self.unit = lat, lon, unit

    def fetch(self):
        r = self.session.get(self.URL, params={
            "latitude": self.lat, "longitude": self.lon,
            "current": "temperature_2m,weather_code,is_day",
            "temperature_unit": self.unit}, timeout=15)
        r.raise_for_status()
        cur = r.json()["current"]
        return {"temp": float(cur["temperature_2m"]), "code": int(cur.get("weather_code") or 0),
                "is_day": bool(cur.get("is_day", 1))}


class FearGreed(Feed):
    """Crypto Fear & Greed index from alternative.me (free, no key, updates daily)."""
    URL = "https://api.alternative.me/fng/"
    refresh_every = 3600

    def fetch(self):
        r = self.session.get(self.URL, params={"limit": 31}, timeout=15)
        r.raise_for_status()
        rows = r.json()["data"]  # newest first
        values = [int(x["value"]) for x in rows]
        return {
            "value": values[0],
            "label": rows[0]["value_classification"],
            "yesterday": values[1] if len(values) > 1 else None,
            "history": list(reversed(values)),  # oldest -> newest, ~30 days
        }


# Yahoo Finance's chart endpoint: free and keyless, but unofficial, so it can
# change or throttle without notice. Failures just hide the indices screen.
DEFAULT_INDICES = [("^GSPC", "S&P"), ("^DJI", "DOW"), ("^IXIC", "NAS")]


class Indices(Feed):
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
    refresh_every = 300

    def __init__(self, indices=None):
        super().__init__()
        self.indices = indices or DEFAULT_INDICES

    def fetch(self):
        out = []
        for symbol, label in self.indices:
            r = self.session.get(self.URL.format(symbol),
                                 params={"range": "1d", "interval": "5m"}, timeout=15)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            meta = res["meta"]
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            closes = [c for c in (res.get("indicators", {}).get("quote", [{}])[0].get("close") or [])
                      if c is not None]
            if price is None or not prev:
                raise ValueError(f"incomplete data for {symbol}")
            out.append({"label": label, "price": float(price),
                        "change": (price - prev) / prev * 100, "points": price - prev,
                        "prev_close": float(prev), "intraday": closes})
        return out


def parse_indices(spec):
    """'^GSPC:S&P,^DJI:DOW' -> [('^GSPC','S&P'), ...]; blank -> defaults."""
    items = []
    for part in (spec or "").split(","):
        sym, _, label = part.strip().partition(":")
        if sym:
            items.append((sym.upper(), (label or sym.lstrip("^"))[:4].upper()))
    return items or DEFAULT_INDICES
