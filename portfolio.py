"""Portfolio value from the prices the ticker already fetches (no extra API calls).

Holdings live only in settings.env on the Pi (gitignored, mode 600):
    PORTFOLIO_HOLDINGS=btc:0.5:30000,ada:12000:4000
i.e. symbol:quantity[:cost basis, the total dollars paid]. Only coins on the coin list are
priced; others are ignored until they're added to it.
"""

import re

SYM_RE = re.compile(r"[a-z0-9.-]{1,15}")
MAX_HOLDINGS = 40


def _number(text):
    text = text.strip().replace("$", "").replace("_", "")
    if not re.fullmatch(r"\d{1,15}(\.\d{1,12})?|\.\d{1,12}", text):
        raise ValueError("not a number")
    return float(text)


def parse_holdings(spec, strict=False):
    """'btc:0.5:30000,ada:12000' -> {'btc': (0.5, 30000.0), 'ada': (12000.0, None)}.
    strict=True raises ValueError on a bad entry (phone page); otherwise bad entries are
    dropped quietly (so a value never ends up in a log line)."""
    out = {}
    for part in (spec or "").split(","):
        part = part.strip().lower()
        if not part:
            continue
        bits = part.split(":")
        try:
            if len(bits) not in (2, 3) or not SYM_RE.fullmatch(bits[0]):
                raise ValueError("expected symbol:quantity[:cost]")
            qty = _number(bits[1])
            cost = _number(bits[2]) if len(bits) == 3 and bits[2].strip() else None
        except ValueError:
            if strict:
                raise ValueError(f"check the entry for {bits[0][:10].upper()}") from None
            continue
        if qty > 0:
            out[bits[0]] = (qty, cost)
    if strict and len(out) > MAX_HOLDINGS:
        raise ValueError(f"{MAX_HOLDINGS} holdings max")
    return out


def format_holdings(holdings):
    def num(x):
        return f"{x:.12f}".rstrip("0").rstrip(".")
    return ",".join(f"{s}:{num(q)}" + (f":{num(c)}" if c is not None else "")
                    for s, (q, c) in holdings.items())


def summarize(assets, holdings):
    """Totals over the held coins that have a price. None if nothing is priced.

    value: current total; day_change / day_pct: since 24 hours ago; cost, gain, gain_pct:
    over the coins with a cost basis only (None if none has one); top: (symbol, 24h %) of
    the best performer among the holdings."""
    total = prev = 0.0
    cost = basis_value = 0.0
    has_cost = False
    best = None
    for a in assets:
        h = holdings.get(a["symbol"].lower())
        if not h or a.get("price") is None:
            continue
        qty, paid = h
        value = qty * a["price"]
        ch = a.get("change_24h")
        total += value
        prev += value / (1 + ch / 100) if ch is not None and ch > -100 else value
        if paid is not None:
            has_cost = True
            cost += paid
            basis_value += value
        if ch is not None and (best is None or ch > best[1]):
            best = (a["symbol"].upper(), ch)
    if total <= 0:
        return None
    day_change = total - prev
    out = {"value": total, "day_change": day_change,
           "day_pct": day_change / prev * 100 if prev else 0.0,
           "cost": None, "gain": None, "gain_pct": None, "top": best}
    if has_cost and cost > 0:
        out.update(cost=cost, gain=basis_value - cost, gain_pct=(basis_value - cost) / cost * 100)
    return out
