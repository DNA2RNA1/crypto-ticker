"""Extra data for non-coin screens: Fear & Greed index and stock market indices.

Neither uses CoinGecko, so they don't count against the monthly CoinGecko budget.
Each keeps its last good data; if a source fails, its screen is skipped (never a crash).
"""

import json
import logging
import shutil
import subprocess
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
        if self.lat is None or self.lon is None:
            return None  # no location set: the clock shows no weather, the weather screen is skipped
        r = self.session.get(self.URL, params={
            "latitude": self.lat, "longitude": self.lon,
            "current": "temperature_2m,weather_code,is_day",
            "hourly": "temperature_2m",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code,sunrise,sunset",
            "forecast_days": 6, "timezone": "auto",
            "temperature_unit": self.unit}, timeout=15)
        r.raise_for_status()
        return parse_weather(r.json())


def _finite(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) < 1000 else None


def parse_weather(js):
    """Open-Meteo forecast JSON -> {temp, code, is_day, high, low, hourly}.

    temp/code/is_day are required (the clock uses them); high/low and the next-12-hours
    list are optional extras for the weather screen and are None/[] if missing or odd.
    Times are local (the request uses timezone=auto)."""
    cur = js["current"]
    temp = _finite(cur["temperature_2m"])
    if temp is None:
        raise ValueError("no current temperature")
    out = {"temp": temp, "code": int(_finite(cur.get("weather_code")) or 0),
           "is_day": bool(cur.get("is_day", 1)), "high": None, "low": None, "hourly": []}
    try:  # the place's own local hour, so another city's forecast row is labeled in its time
        out["hour"] = int(str(cur.get("time", ""))[11:13])
    except ValueError:
        pass
    try:
        daily = js.get("daily") or {}
        out["high"] = _finite((daily.get("temperature_2m_max") or [None])[0])
        out["low"] = _finite((daily.get("temperature_2m_min") or [None])[0])
    except (AttributeError, TypeError, IndexError):
        pass
    try:  # the next days (TV forecast) and today's sunrise/sunset, local "2026-11-08T06:58"
        daily = js.get("daily") or {}
        dates = daily.get("time") or []
        his, los = daily.get("temperature_2m_max") or [], daily.get("temperature_2m_min") or []
        codes = daily.get("weather_code") or []
        days = []
        for i, d in enumerate(dates[:6]):
            hi, lo = _finite(his[i]) if i < len(his) else None, _finite(los[i]) if i < len(los) else None
            if hi is not None and lo is not None and isinstance(d, str):
                days.append({"date": d[:10], "high": hi, "low": lo,
                             "code": int(_finite(codes[i]) or 0) if i < len(codes) else 0})
        out["days"] = days
        rise, sets = daily.get("sunrise") or [], daily.get("sunset") or []
        if rise and sets and isinstance(rise[0], str) and isinstance(sets[0], str):
            out["sunrise"], out["sunset"] = rise[0][:16], sets[0][:16]
    except (AttributeError, TypeError, IndexError, ValueError):
        pass
    try:
        hourly = js.get("hourly") or {}
        times, temps = hourly.get("time") or [], hourly.get("temperature_2m") or []
        now_hour = str(cur.get("time", ""))[:13]  # "2026-11-02T14"
        start = next((i for i, t in enumerate(times) if str(t)[:13] >= now_hour), None)
        if start is not None and now_hour:
            pts = [_finite(v) for v in temps[start:start + 13]]  # now + the next 12 hours
            out["hourly"] = [v for v in pts if v is not None] if None not in pts else []
    except (AttributeError, TypeError):
        pass
    return out


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
    # query2 first: on some networks query1 answers every request with 429 (seen 2026-11),
    # while query2 serves the same data. Each request falls through to the other host.
    HOSTS = ("query2", "query1")
    URL = "https://{}.finance.yahoo.com/v8/finance/chart/{}"
    refresh_every = 300

    # Yahoo refuses (429) Python's requests from some networks even when curl, with this
    # same short browser name, gets the data (seen on the Pi, 2026-11). So: requests first,
    # then the same URL through curl, on each host.
    UA = "Mozilla/5.0"

    def __init__(self, indices=None):
        super().__init__()
        self.indices = indices or DEFAULT_INDICES
        self.session.headers["User-Agent"] = self.UA

    def _get_json(self, url, params):
        try:
            r = self.session.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as first:
            if not shutil.which("curl"):
                raise
            full = url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
            try:
                out = subprocess.run(
                    ["curl", "-sS", "--fail", "-m", "15", "-A", self.UA, full],
                    capture_output=True, timeout=20, check=True).stdout
                return json.loads(out)
            except Exception as second:
                raise RuntimeError(f"{first}; curl: {second}") from None

    def chart(self, symbol):
        """One symbol's latest price, previous close and today's 5-minute closes."""
        last = None
        for host in self.HOSTS:
            try:
                data = self._get_json(self.URL.format(host, symbol), {"range": "1d", "interval": "5m"})
                res = data["chart"]["result"][0]
                break
            except Exception as exc:  # try the other host before giving up
                last = exc
        else:
            raise last
        meta = res["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        closes = [c for c in (res.get("indicators", {}).get("quote", [{}])[0].get("close") or [])
                  if c is not None]
        if price is None or not prev:
            raise ValueError(f"incomplete data for {symbol}")
        return {"price": float(price), "change": (price - prev) / prev * 100,
                "points": price - prev, "prev_close": float(prev), "intraday": closes}

    def fetch(self):
        return [{"label": label, **self.chart(symbol)} for symbol, label in self.indices]


# Gold and silver: spot first, futures (very close to spot) if Yahoo has no spot quote.
# Oil: WTI crude front-month futures.
METALS = [(("XAUUSD=X", "GC=F"), "GOLD"), (("XAGUSD=X", "SI=F"), "SILVER"), (("CL=F",), "OIL")]


class Metals(Indices):
    """Gold, silver and WTI oil prices from Yahoo. One failing doesn't hide the others."""

    def __init__(self):
        super().__init__([])

    def fetch(self):
        out = []
        for symbols, label in METALS:
            for symbol in symbols:
                try:
                    out.append({"label": label, "symbol": symbol, **self.chart(symbol)})
                    break
                except Exception as exc:
                    log.info("metals: %s unavailable (%s)", symbol, exc)
        if not out:
            raise ValueError("no gold, silver or oil price")
        return out


def parse_indices(spec):
    """'^GSPC:S&P,^DJI:DOW' -> [('^GSPC','S&P'), ...]; blank -> defaults."""
    items = []
    for part in (spec or "").split(","):
        sym, _, label = part.strip().partition(":")
        if sym:
            items.append((sym.upper(), (label or sym.lstrip("^"))[:4].upper()))
    return items or DEFAULT_INDICES


def geocode(query, session=None):
    """Places matching a ZIP code or city name (Open-Meteo geocoding, free, no key)."""
    query = query.strip()
    params = {"name": query, "count": 5, "language": "en", "format": "json"}
    if query.isdigit() and len(query) == 5:  # US ZIP code
        params["countryCode"] = "US"
    s = session or requests.Session()
    r = s.get("https://geocoding-api.open-meteo.com/v1/search", params=params, timeout=15)
    r.raise_for_status()
    out = []
    for p in r.json().get("results", []) or []:
        label = ", ".join(x for x in (p.get("name"), p.get("admin1"), p.get("country_code")) if x)
        out.append({"label": label, "lat": round(float(p["latitude"]), 4),
                    "lon": round(float(p["longitude"]), 4), "timezone": p.get("timezone", "")})
    return out
