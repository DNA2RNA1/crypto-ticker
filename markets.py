"""Prediction-market odds (the "odds" screen): Polymarket events and Kalshi markets.

Display only. You paste links to events you want to follow; the panel shows the question
and the leading outcomes with their implied probability (the last traded price: 62c on a
Yes share = 62%). Nothing here places, links to or encourages bets.

    Polymarket  https://polymarket.com/event/<slug>     (Gamma API, public, no key)
    Kalshi      https://kalshi.com/markets/.../<TICKER>  (Trade API v2 public market data, no key)

Each entry keeps its last good data; a failing source never empties the screen.
"""

import json
import logging
import re
import threading
import time
from urllib.parse import urlparse

from feeds import Feed

log = logging.getLogger("ticker.markets")

POLY_EVENTS = "https://gamma-api.polymarket.com/events"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
MAX_ENTRIES = 10
MAX_OUTCOMES = 4
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9\-]{0,150}")
TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.\-_]{1,80}")
TAG_COLORS = {"poly": (46, 92, 255), "kalshi": (0, 190, 140)}
TAG_NAMES = {"poly": "POLYMARKET", "kalshi": "KALSHI"}


def parse_entry(text):
    """A pasted link or short form -> canonical 'poly:<slug>' or 'kalshi:<TICKER>'.

    Accepts polymarket.com/event/<slug>[/<market>], kalshi.com/markets/<series>/<...>/<ticker>,
    'poly:<slug>' and 'kalshi:<ticker>'. Raises ValueError for anything else."""
    t = (text or "").strip()
    low = t.lower()
    if low.startswith("poly:"):
        slug = low[5:].strip()
    elif low.startswith("kalshi:"):
        return _kalshi(t[7:].strip())
    else:
        url = urlparse(t if "://" in t else "https://" + t)
        host = (url.hostname or "").lower()
        parts = [p for p in url.path.split("/") if p]
        if host in ("polymarket.com", "www.polymarket.com"):
            if len(parts) >= 2 and parts[0] == "event":
                slug = parts[1].lower()
            else:
                raise ValueError("use a Polymarket event link: polymarket.com/event/...")
        elif host in ("kalshi.com", "www.kalshi.com"):
            if len(parts) >= 2 and parts[0] in ("markets", "events"):
                return _kalshi(parts[-1])
            raise ValueError("use a Kalshi market link: kalshi.com/markets/...")
        else:
            raise ValueError("paste a polymarket.com/event/... or kalshi.com/markets/... link")
    if not SLUG_RE.fullmatch(slug):
        raise ValueError("that Polymarket link has an odd event name")
    return f"poly:{slug}"


def _kalshi(ticker):
    ticker = ticker.strip().upper()
    if not TICKER_RE.fullmatch(ticker):
        raise ValueError("that Kalshi link has an odd market ticker")
    return f"kalshi:{ticker}"


def parse_entries(raw):
    """Links, one per line or comma separated -> canonical entries (duplicates dropped).
    Raises ValueError on a bad one or more than MAX_ENTRIES."""
    out = []
    for part in re.split(r"[\s,]+", raw or ""):
        if part:
            e = parse_entry(part)
            if e not in out:
                out.append(e)
    if len(out) > MAX_ENTRIES:
        raise ValueError(f"at most {MAX_ENTRIES} markets")
    return out


def entries_from_env(raw):
    out = []
    for part in (raw or "").split(","):
        if not part.strip():
            continue
        try:
            e = parse_entry(part)
        except ValueError as exc:
            log.warning("ignoring odds market %r: %s", part.strip(), exc)
            continue
        if e not in out:
            out.append(e)
    return out[:MAX_ENTRIES]


# --- parsing ------------------------------------------------------------------------
def _prob(v):
    """0.62, "0.62" -> 0.62; anything else (or outside 0-1) -> None."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if 0.0 <= f <= 1.0 else None


def _json_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
        return v if isinstance(v, list) else []
    return []


def _clean(text, limit=90):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit]


def parse_polymarket(data):
    """Gamma ``/events?slug=`` response (a list of events) -> {"title", "outcomes", "closed"}.

    One market: its own outcomes and prices (Yes 62%, No 38%). Several markets (one per
    candidate or team): each open market's name (``groupItemTitle``) with its Yes price."""
    events = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    ev = next((e for e in events if isinstance(e, dict)), None)
    if not ev:
        raise ValueError("no such Polymarket event")
    markets = [m for m in ev.get("markets") or [] if isinstance(m, dict)]
    if not markets:
        raise ValueError("Polymarket event has no markets")
    outcomes = []
    if len(markets) == 1:
        m = markets[0]
        names, prices = _json_list(m.get("outcomes")), _json_list(m.get("outcomePrices"))
        for name, price in zip(names, prices):
            p = _prob(price)
            if p is not None:
                outcomes.append((_clean(name, 40), p))
        title = ev.get("title") or m.get("question")
    else:
        for m in markets:
            if m.get("closed") or m.get("active") is False:
                continue
            names, prices = _json_list(m.get("outcomes")), _json_list(m.get("outcomePrices"))
            yes = next((i for i, n in enumerate(names) if str(n).lower() == "yes"), 0)
            p = _prob(prices[yes]) if len(prices) > yes else None
            name = m.get("groupItemTitle") or m.get("question")
            if p is not None and name:
                outcomes.append((_clean(name, 40), p))
        title = ev.get("title")
    if not outcomes:
        raise ValueError("Polymarket event has no prices")
    outcomes.sort(key=lambda o: -o[1])
    closed = bool(ev.get("closed")) or all(m.get("closed") for m in markets)
    return {"title": _clean(title) or "Polymarket", "outcomes": outcomes[:MAX_OUTCOMES], "closed": closed}


def _kalshi_price(m):
    """Yes probability of one Kalshi market: last trade, else the bid/ask midpoint.
    Prices come in cents (``last_price``: 62) or dollars (``last_price_dollars``: "0.6200");
    both are read, as the API has used both."""
    for key in ("last_price_dollars", "yes_price_dollars"):
        p = _prob(m.get(key))
        if p:
            return p
    for key in ("last_price", "yes_price"):
        v = m.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 100:
            return v / 100
    bid, ask = m.get("yes_bid_dollars"), m.get("yes_ask_dollars")
    bid, ask = _prob(bid), _prob(ask)
    if bid is None and isinstance(m.get("yes_bid"), (int, float)):
        bid = m["yes_bid"] / 100 if 0 <= m["yes_bid"] <= 100 else None
    if ask is None and isinstance(m.get("yes_ask"), (int, float)):
        ask = m["yes_ask"] / 100 if 0 <= m["yes_ask"] <= 100 else None
    if bid is not None and ask and ask >= bid:
        return (bid + ask) / 2
    return None


def parse_kalshi(data):
    """``/events/<T>?with_nested_markets=true`` ({"event": {..., "markets": [...]}}) or
    ``/markets/<T>`` ({"market": {...}}) -> {"title", "outcomes", "closed"}."""
    if not isinstance(data, dict):
        raise ValueError("odd Kalshi response")
    ev = data.get("event") if isinstance(data.get("event"), dict) else None
    if ev is not None:
        markets = ev.get("markets") or data.get("markets") or []
        title = ev.get("title")
    elif isinstance(data.get("market"), dict):
        markets = [data["market"]]
        title = data["market"].get("title")
    else:
        raise ValueError("no such Kalshi market")
    markets = [m for m in markets if isinstance(m, dict)]
    outcomes = []
    single = len(markets) == 1
    for m in markets:
        if str(m.get("status", "")).lower() in ("settled", "finalized", "determined") and not single:
            continue
        p = _kalshi_price(m)
        if p is None:
            continue
        if single:
            outcomes += [("Yes", p), ("No", 1 - p)]
        else:
            name = m.get("yes_sub_title") or m.get("subtitle") or m.get("title")
            if name:
                outcomes.append((_clean(name, 40), p))
    if not outcomes:
        raise ValueError("Kalshi market has no prices")
    outcomes.sort(key=lambda o: -o[1])
    closed = all(str(m.get("status", "")).lower() in ("closed", "settled", "finalized", "determined")
                 for m in markets)
    return {"title": _clean(title) or "Kalshi", "outcomes": outcomes[:MAX_OUTCOMES], "closed": closed}


# --- the feed -----------------------------------------------------------------------
class Markets(Feed):
    """Background refresh every 5 minutes; ``rows()`` gives the last good data of every
    entry that has some, in the order pasted."""
    refresh_every = 300

    def __init__(self, entries):
        super().__init__()
        self.entries = list(entries)
        self.last = {}            # entry -> last good {"title", "outcomes", "closed", "fetched"}
        self.status = {}          # entry -> {"ok": True, "title"} or {"ok": False, "error"}
        self._lock = threading.Lock()
        self._next = 0

    def _get(self, url, params=None):
        r = self.session.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def market(self, entry):
        kind, _, key = entry.partition(":")
        if kind == "poly":
            return parse_polymarket(self._get(POLY_EVENTS, {"slug": key}))
        try:  # an event ticker (several outcomes) first, then a single market
            return parse_kalshi(self._get(f"{KALSHI}/events/{key}", {"with_nested_markets": "true"}))
        except Exception as first:
            try:
                return parse_kalshi(self._get(f"{KALSHI}/markets/{key}"))
            except Exception:
                raise first from None

    def fetch(self):
        now = time.time()
        ok = 0
        for entry in self.entries:
            try:
                m = self.market(entry)
            except Exception as exc:
                msg = str(exc)[:120] or type(exc).__name__
                log.info("odds: %s unavailable (%s)", entry, msg)
                with self._lock:
                    self.status[entry] = {"ok": False, "error": msg, "had": entry in self.last}
                continue
            ok += 1
            with self._lock:
                self.last[entry] = {**m, "entry": entry, "source": entry.partition(":")[0], "fetched": now}
                self.status[entry] = {"ok": True, "title": m["title"], "closed": m["closed"]}
        if self.entries and not ok and not self.last:
            raise ValueError("no prediction-market data")
        return self.rows()

    def rows(self, now=None):
        now = now or time.time()
        with self._lock:
            out = []
            for e in self.entries:
                m = self.last.get(e)
                if m:
                    failed = not self.status.get(e, {}).get("ok", True)
                    out.append({**m, "stale": failed or now - m["fetched"] > 3 * self.refresh_every})
            return out

    def next_batch(self, rows, n):
        if not rows or n >= len(rows):
            return list(rows)
        start = self._next % len(rows)
        self._next += n
        return [rows[(start + i) % len(rows)] for i in range(n)]

    def status_for(self, entries):
        with self._lock:
            return {e: self.status.get(e) for e in entries}
