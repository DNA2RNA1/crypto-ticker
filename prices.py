"""Price data from CoinGecko's /coins/markets endpoint.

A single request returns price, 24h change, a 7-day sparkline and the coin's
icon URL for every symbol, so each refresh costs one API call.
"""

import logging

import requests

log = logging.getLogger("ticker.prices")

API = "https://api.coingecko.com/api/v3"
PRO_API = "https://pro-api.coingecko.com/api/v3"


class PriceError(Exception):
    pass


class CoinGecko:
    def __init__(self, symbols, currency="usd", api_key="", pro=False, timeout=15, on_call=None):
        """symbols: list of entries, each either a ticker ("btc") or
        "ticker:coingecko-id" ("ada:cardano") to pin an exact coin."""
        self.currency = currency.lower()
        self.timeout = timeout
        self.on_call = on_call  # called once per API request (for the monthly budget)
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

    def _markets(self, **params):
        params.update(vs_currency=self.currency, sparkline="true",
                      price_change_percentage="24h")
        if self.on_call:
            self.on_call()
        try:
            r = self.session.get(f"{self.base}/coins/markets", params=params,
                                 timeout=self.timeout)
        except requests.RequestException as exc:
            raise PriceError(f"network error: {exc}") from exc
        if r.status_code == 429:
            raise PriceError("rate limited by CoinGecko (HTTP 429)")
        if r.status_code != 200:
            raise PriceError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as exc:
            raise PriceError("bad JSON from CoinGecko") from exc

    def fetch(self):
        """Return a list of asset dicts in the user's order.

        Each dict: symbol, name, price (float), change_24h (float or None),
        sparkline (list of floats), image (url or None).
        """
        plain = [s for s, i in self.order if i is None]
        pinned = [i for _, i in self.order if i is not None]

        rows = []
        if plain:
            # include_tokens=top (the default) returns the largest coin for each
            # ticker, so "btc" means Bitcoin, not a copycat token.
            rows += self._markets(symbols=",".join(plain), include_tokens="top")
        if pinned:
            rows += self._markets(ids=",".join(pinned))

        by_id = {row["id"]: row for row in rows}
        by_sym = {}
        for row in rows:
            by_sym.setdefault(row["symbol"].lower(), row)

        assets = []
        for sym, coin_id in self.order:
            row = by_id.get(coin_id) if coin_id else by_sym.get(sym)
            if row is None or row.get("current_price") is None:
                log.warning("no price data for %s", coin_id or sym)
                continue
            spark = (row.get("sparkline_in_7d") or {}).get("price") or []
            assets.append({
                "symbol": sym.upper(),
                "id": row["id"],
                "name": row.get("name", sym.upper()),
                "price": float(row["current_price"]),
                "change_24h": row.get("price_change_percentage_24h"),
                "sparkline": [float(p) for p in spark if p is not None],
                "image": row.get("image"),
            })
        if not assets:
            raise PriceError("no usable price data returned")
        return assets

    def search(self, query, limit=12):
        """Coins matching a name or ticker, best-ranked first."""
        if self.on_call:
            self.on_call()
        try:
            r = self.session.get(f"{self.base}/search", params={"query": query},
                                 timeout=self.timeout)
        except requests.RequestException as exc:
            raise PriceError(f"network error: {exc}") from exc
        if r.status_code != 200:
            raise PriceError(f"HTTP {r.status_code}")
        coins = r.json().get("coins", [])
        coins.sort(key=lambda c: c.get("market_cap_rank") or 10**9)
        return [{"id": c["id"], "name": c.get("name", ""), "symbol": (c.get("symbol") or "").upper(),
                 "market_cap_rank": c.get("market_cap_rank"), "thumb": c.get("large") or c.get("thumb")}
                for c in coins[:limit]]
