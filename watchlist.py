"""Watchlist: stocks, ETFs, mutual funds and futures from Yahoo (the "watchlist" screen).

WATCHLIST is a comma-separated list of Yahoo symbols, each optionally with a short label
for the panel: ``AAPL, VTSAX, ES=F:S&P, CL=F:WTI``. Packs of ready-made symbols (futures
groups, popular funds) can be added from the phone page.

One request per symbol (today's 5-minute chart). Mutual funds price once a day after the
close, so they show their net asset value (NAV) with a one-month chart instead. A symbol
whose last trade is more than 90 minutes old is marked CLOSED (overnight, weekends, a
holiday). One symbol failing never hides the others; each keeps its last good quote.
"""

import logging
import re
import threading
import time

from feeds import Indices

log = logging.getLogger("ticker.watchlist")

MAX_SYMBOLS = 20
SYMBOL_RE = re.compile(r"[A-Z0-9^][A-Z0-9.^=\-]{0,14}")
LABEL_RE = re.compile(r"[A-Z0-9&.\-]{1,6}")
CLOSED_AFTER = 90 * 60          # seconds since the last trade before a quote reads CLOSED
REFRESH_CHOICES = (5, 10, 15, 30)
CLOSED_REFRESH = 30 * 60        # everything closed: check every 30 minutes
SIGNED_TYPES = {"EQUITY", "ETF", "MUTUALFUND"}   # priced in money; futures and indices aren't

# Ready-made groups for the phone page. Labels are at most 6 characters (the panel width).
PACKS = [
    ("Stock index futures", "ES=F:S&P,NQ=F:NASDQ,YM=F:DOW,RTY=F:RUSSL"),
    ("Energy", "CL=F:WTI,BZ=F:BRENT,NG=F:NATGAS,RB=F:GASOL,HO=F:HEAT"),
    ("Metals", "GC=F:GOLD,SI=F:SILVER,HG=F:COPPER,PL=F:PLAT,PA=F:PALLAD"),
    ("Grains & softs", "ZC=F:CORN,ZW=F:WHEAT,ZS=F:SOY,KC=F:COFFEE,SB=F:SUGAR,CC=F:COCOA,CT=F:COTTON"),
    ("Livestock", "LE=F:CATTLE,HE=F:HOGS"),
    ("Rates & dollar", "ZN=F:TNOTE,ZB=F:TBOND,^TNX:10Y,DX-Y.NYB:DXY"),
    ("Crypto & volatility", "BTC=F:BTC-F,ETH=F:ETH-F,^VIX:VIX"),
    ("Popular index funds", "VTSAX,VFIAX,FXAIX,VTIAX,VBTLX"),
    ("Popular ETFs", "SPY,QQQ,VTI,DIA,IWM"),
]


def default_label(symbol):
    """'ES=F' -> 'ES', '^VIX' -> 'VIX', 'DX-Y.NYB' -> 'DX-Y', 'BRK-B' -> 'BRK-B'."""
    s = symbol.lstrip("^")
    s = s.split("=")[0]
    if "." in s and not s.startswith("."):
        s = s.split(".")[0]
    return s[:6] or symbol[:6]


def parse_entry(text):
    """'es=f:s&p' -> ('ES=F', 'S&P'); 'aapl' -> ('AAPL', 'AAPL'). Raises ValueError."""
    sym, sep, label = text.strip().upper().partition(":")
    sym, label = sym.strip(), label.strip()
    if not SYMBOL_RE.fullmatch(sym):
        raise ValueError(f"{text.strip()!r} isn't a Yahoo symbol (e.g. AAPL, VTSAX, ES=F, ^VIX)")
    if sep and not LABEL_RE.fullmatch(label):
        raise ValueError(f"label for {sym} must be 1-6 letters, digits or & . -")
    return sym, label or default_label(sym)


def parse_entries(raw):
    """'AAPL, ES=F:S&P' -> [('AAPL','AAPL'), ('ES=F','S&P')]. Duplicates are dropped
    (first wins). Raises ValueError on a bad entry or more than MAX_SYMBOLS."""
    out, seen = [], set()
    for part in (raw or "").replace("\n", ",").split(","):
        if not part.strip():
            continue
        sym, label = parse_entry(part)
        if sym in seen:
            continue
        seen.add(sym)
        out.append((sym, label))
    if len(out) > MAX_SYMBOLS:
        raise ValueError(f"at most {MAX_SYMBOLS} symbols")
    return out


def entries_from_env(raw):
    """Like parse_entries, but a bad entry in a hand-edited settings.env is logged and
    skipped (never a crash), and anything past MAX_SYMBOLS is ignored."""
    out, seen = [], set()
    for part in (raw or "").split(","):
        if not part.strip():
            continue
        try:
            sym, label = parse_entry(part)
        except ValueError as exc:
            log.warning("ignoring watchlist entry: %s", exc)
            continue
        if sym not in seen:
            seen.add(sym)
            out.append((sym, label))
    return out[:MAX_SYMBOLS]


def format_entries(entries):
    """Back to the WATCHLIST string, writing a label only when it isn't the default."""
    return ",".join(sym if label == default_label(sym) else f"{sym}:{label}" for sym, label in entries)


def parse_quote(data, symbol, now=None):
    """A Yahoo chart response -> one quote. Raises ValueError if it has no price."""
    now = now or time.time()
    try:
        res = data["chart"]["result"][0]
        meta = res["meta"]
    except (KeyError, IndexError, TypeError):
        err = (data or {}).get("chart", {}).get("error") if isinstance(data, dict) else None
        desc = err.get("description") if isinstance(err, dict) else None
        raise ValueError(desc or f"no data for {symbol}") from None
    if not isinstance(meta, dict):
        raise ValueError(f"no data for {symbol}")
    price = _num(meta.get("regularMarketPrice"))
    prev = _num(meta.get("chartPreviousClose")) or _num(meta.get("previousClose"))
    if price is None or not prev:
        raise ValueError(f"incomplete data for {symbol}")
    kind = str(meta.get("instrumentType") or "").upper()
    cur = str(meta.get("currency") or "").lower()
    traded = _num(meta.get("regularMarketTime"))
    return {
        "symbol": symbol,
        "kind": kind,
        "name": str(meta.get("shortName") or meta.get("longName") or "")[:60],
        "price": price,
        "prev_close": prev,
        "change": (price - prev) / prev * 100,
        "points": price - prev,
        "currency": cur if kind in SIGNED_TYPES else "",
        "intraday": closes(res),
        "traded": traded,
        "closed": bool(traded) and now - traded > CLOSED_AFTER,
    }


def closes(res):
    try:
        raw = res.get("indicators", {}).get("quote", [{}])[0].get("close") or []
    except (AttributeError, IndexError, TypeError):
        return []
    return [float(c) for c in raw if _num(c) is not None]


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if v == v and abs(v) != float("inf") else None


class Watchlist(Indices):
    """Background feed: ``update()`` returns the last good quote of every symbol that has
    one, in WATCHLIST order. Symbols that never loaded are left out (the screen skips
    them); the phone page shows each symbol's status."""

    def __init__(self, entries, refresh_minutes=10):
        super().__init__([])
        self.entries = list(entries)
        self.refresh_minutes = refresh_minutes
        self.refresh_every = refresh_minutes * 60
        self.quotes = {}          # symbol -> last good quote
        self.status = {}          # symbol -> {"ok": True, "name"} or {"ok": False, "error"}
        self._lock = threading.Lock()
        self._next = 0            # rotation position across cycles

    def _chart(self, symbol, params):
        last = None
        for host in self.HOSTS:
            try:
                return self._get_json(self.URL.format(host, symbol), params)
            except Exception as exc:  # try the other host before giving up
                last = exc
        raise last

    def quote(self, symbol, now=None):
        q = parse_quote(self._chart(symbol, {"range": "1d", "interval": "5m"}), symbol, now)
        if q["kind"] == "MUTUALFUND" or len(q["intraday"]) < 2:
            # one price a day: chart the last month of closes instead (the change stays
            # the day's change, from the 1-day request)
            try:
                month = self._chart(symbol, {"range": "1mo", "interval": "1d"})
                pts = closes(month["chart"]["result"][0])
                if len(pts) >= 2:
                    q["intraday"], q["span"] = pts, "1M"
            except Exception as exc:
                log.info("watchlist: no month chart for %s (%s)", symbol, exc)
        return q

    def fetch(self):
        now = time.time()
        ok = 0
        for symbol, label in self.entries:
            try:
                q = self.quote(symbol, now)
            except Exception as exc:
                msg = str(exc)[:120] or type(exc).__name__
                log.info("watchlist: %s unavailable (%s)", symbol, msg)
                with self._lock:
                    # a symbol that loaded before keeps its last good quote (shown stale)
                    self.status[symbol] = {"ok": False, "error": msg, "had": symbol in self.quotes}
                continue
            ok += 1
            with self._lock:
                self.quotes[symbol] = {**q, "label": label, "fetched": now}
                self.status[symbol] = {"ok": True, "name": q["name"], "kind": q["kind"]}
        if self.entries and not ok and not self.quotes:
            raise ValueError("no watchlist quotes")
        # all markets closed (night, weekend): no need to ask every few minutes
        with self._lock:
            live = [q for q in self.quotes.values() if not q["closed"]]
        if self.quotes and not live:
            self.next_fetch = max(self.next_fetch, now + CLOSED_REFRESH)
        return self.rows()

    def rows(self, now=None):
        """Quotes in WATCHLIST order; ``stale`` marks one whose latest refresh failed or
        that is much older than the refresh interval."""
        now = now or time.time()
        out = []
        with self._lock:
            for symbol, label in self.entries:
                q = self.quotes.get(symbol)
                if not q:
                    continue
                failed = not self.status.get(symbol, {}).get("ok", True)
                old = now - q["fetched"] > max(3 * self.refresh_every, CLOSED_REFRESH + 600)
                out.append({**q, "label": label, "stale": failed or old})
        return out

    def next_batch(self, rows, n):
        """The next ``n`` quotes, rotating across cycles (n=0: all of them)."""
        if not rows:
            return []
        if not n or n >= len(rows):
            return list(rows)
        start = self._next % len(rows)
        self._next += n
        return [rows[(start + i) % len(rows)] for i in range(n)]

    def status_for(self, entries):
        with self._lock:
            return {sym: self.status.get(sym) for sym, _ in entries}
