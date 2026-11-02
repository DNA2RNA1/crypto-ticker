"""Price data from CoinGecko's /coins/markets endpoint.

A single request returns price, 24h change, a 7-day sparkline and the coin's
icon URL for every symbol, so each refresh costs one API call.

Failure handling:
  * timeouts, connection errors (Wi-Fi down) and HTTP 5xx are retried a couple of
    times with exponential backoff, then raised as ``NetworkError``;
  * HTTP 429 is not retried on the spot (that only makes throttling worse); it raises
    ``RateLimitError`` carrying the server's Retry-After, and the ticker waits;
  * malformed JSON or rows missing fields raise ``DataError`` / are skipped, never crash.
Successful responses are cached (memory + disk) so the last good prices survive a
reboot without internet; see ``last_good()``.
"""

import logging
import math
import time

import requests

from cache import RequestCache, make_key
from retry import Retryable, retry_call

log = logging.getLogger("ticker.prices")

API = "https://api.coingecko.com/api/v3"
PRO_API = "https://pro-api.coingecko.com/api/v3"

MARKETS_TTL = 50      # seconds; CoinGecko's free data only changes about once a minute
SEARCH_TTL = 3600


class PriceError(Exception):
    """Any failure to get usable prices."""


class NetworkError(PriceError, Retryable):
    """Timeout, DNS failure, no route (Wi-Fi down) or a server-side 5xx error."""


class RateLimitError(PriceError):
    """HTTP 429. ``retry_after`` is the server's hint in seconds, or None."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class DataError(PriceError):
    """The response arrived but wasn't the JSON we expected."""


def _retry_after(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _num(value):
    """float(value) if it's a real finite number, else None."""
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


class CoinGecko:
    def __init__(self, symbols, currency="usd", api_key="", pro=False, timeout=(5, 10),
                 on_call=None, cache=None, attempts=3, sleep=time.sleep):
        """symbols: list of entries, each either a ticker ("btc") or
        "ticker:coingecko-id" ("ada:cardano") to pin an exact coin.
        timeout: (connect, read) seconds per attempt."""
        self.currency = currency.lower()
        self.timeout = timeout
        self.on_call = on_call  # called once per API request (for the monthly budget)
        self.cache = cache if cache is not None else RequestCache()
        self.attempts = attempts
        self.sleep = sleep
        self.base = PRO_API if pro else API
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "crypto-ticker/2.0"
        if api_key:
            header = "x-cg-pro-api-key" if pro else "x-cg-demo-api-key"
            self.session.headers[header] = api_key

        # Keep the user's order; split into plain tickers and pinned ids.
        self.order = []  # list of (symbol, id or None)
        for entry in symbols:
            entry = entry.strip().lower()
            if not entry:
                continue
            sym, _, coin_id = entry.partition(":")
            self.order.append((sym, coin_id or None))

    # --- HTTP -------------------------------------------------------------
    def _get_once(self, path, params):
        if self.on_call:
            self.on_call()
        try:
            r = self.session.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        except requests.Timeout as exc:
            raise NetworkError(f"timed out: {exc}") from exc
        except requests.RequestException as exc:  # DNS failure, refused, no route...
            raise NetworkError(f"network error: {exc}") from exc
        if r.status_code == 429:
            wait = _retry_after(r.headers.get("Retry-After"))
            raise RateLimitError("rate limited by CoinGecko (HTTP 429)", wait)
        if 500 <= r.status_code < 600:
            raise NetworkError(f"CoinGecko server error HTTP {r.status_code}")
        if r.status_code != 200:
            raise PriceError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except ValueError as exc:
            raise DataError("bad JSON from CoinGecko") from exc
        # CoinGecko sometimes reports throttling as a 200 with an error body.
        if isinstance(data, dict) and isinstance(data.get("status"), dict):
            status = data["status"]
            if status.get("error_code") == 429:
                raise RateLimitError("rate limited by CoinGecko (HTTP 429)")
            raise PriceError(f"CoinGecko error: {str(status.get('error_message'))[:200]}")
        return data

    def _markets_requests(self):
        plain = [s for s, i in self.order if i is None]
        pinned = [i for _, i in self.order if i is not None]
        common = dict(vs_currency=self.currency, sparkline="true", price_change_percentage="24h")
        reqs = []
        if plain:
            # include_tokens=top (the default) returns the largest coin for each
            # ticker, so "btc" means Bitcoin, not a copycat token.
            reqs.append(dict(common, symbols=",".join(plain), include_tokens="top"))
        if pinned:
            reqs.append(dict(common, ids=",".join(pinned)))
        return reqs

    def _markets(self, params):
        key = make_key(self.base, "/coins/markets", **params)
        hit = self.cache.get(key, MARKETS_TTL)
        if hit is not None:
            return hit
        data = retry_call(lambda: self._get_once("/coins/markets", params), attempts=self.attempts,
                          retry_on=(NetworkError,), sleep=self.sleep, what="price request")
        if not isinstance(data, list):
            raise DataError(f"expected a list of coins, got {type(data).__name__}")
        self.cache.put(key, data, persist=True)
        return data

    # --- parsing ----------------------------------------------------------
    def _assets(self, rows):
        rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)]
        by_id = {row["id"]: row for row in rows}
        by_sym = {}
        for row in rows:
            if isinstance(row.get("symbol"), str):
                by_sym.setdefault(row["symbol"].lower(), row)

        assets = []
        for sym, coin_id in self.order:
            row = by_id.get(coin_id) if coin_id else by_sym.get(sym)
            price = _num(row.get("current_price")) if row else None
            if price is None:
                log.warning("no price data for %s", coin_id or sym)
                continue
            spark = row.get("sparkline_in_7d")
            spark = spark.get("price") if isinstance(spark, dict) else None
            spark = [p for p in (_num(x) for x in spark) if p is not None] if isinstance(spark, list) else []
            name = row.get("name")
            image = row.get("image")
            assets.append({
                "symbol": sym.upper(),
                "id": row["id"],
                "name": name if isinstance(name, str) else sym.upper(),
                "price": price,
                "change_24h": _num(row.get("price_change_percentage_24h")),
                "sparkline": spark,
                "image": image if isinstance(image, str) else None,
                "updated": row.get("last_updated") if isinstance(row.get("last_updated"), str) else None,
            })
        return assets

    # --- public -----------------------------------------------------------
    def fetch(self):
        """Return a list of asset dicts in the user's order.

        Each dict: symbol, name, price (float), change_24h (float or None),
        sparkline (list of floats), image (url or None).
        Raises PriceError (or a subclass) if nothing usable came back.
        """
        rows = []
        for params in self._markets_requests():
            rows += self._markets(params)
        assets = self._assets(rows)
        if not assets:
            raise DataError("no usable price data returned")
        return assets

    def last_good(self):
        """(assets, fetched_at) from the most recent successful responses for this coin
        list, however old (from memory or the on-disk cache), or (None, None)."""
        rows, oldest = [], None
        for params in self._markets_requests():
            hit = self.cache.get_any(make_key(self.base, "/coins/markets", **params))
            if hit is None:
                continue
            stored_at, data = hit
            if isinstance(data, list):
                rows += data
                oldest = stored_at if oldest is None else min(oldest, stored_at)
        assets = self._assets(rows) if rows else []
        return (assets, oldest) if assets else (None, None)

    def search(self, query, limit=12):
        """Coins matching a name or ticker, best-ranked first."""
        query = query.strip()[:64]
        key = make_key(self.base, "/search", query=query.lower())
        data = self.cache.get(key, SEARCH_TTL)
        if data is None:
            data = retry_call(lambda: self._get_once("/search", {"query": query}), attempts=2,
                              retry_on=(NetworkError,), sleep=self.sleep, what="search")
            if not isinstance(data, dict):
                raise DataError("bad search response")
            self.cache.put(key, data)
        coins = [c for c in data.get("coins") or [] if isinstance(c, dict) and isinstance(c.get("id"), str)]
        coins.sort(key=lambda c: c.get("market_cap_rank") if isinstance(c.get("market_cap_rank"), int) else 10**9)
        return [{"id": c["id"], "name": str(c.get("name") or ""), "symbol": str(c.get("symbol") or "").upper(),
                 "market_cap_rank": c.get("market_cap_rank"), "thumb": c.get("large") or c.get("thumb")}
                for c in coins[:limit]]
