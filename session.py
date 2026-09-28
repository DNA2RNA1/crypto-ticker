"""US stock-market session math for tokenized index funds.

Tokenized funds (SPYX, QQQX) trade 24/7, so CoinGecko's rolling "24h change" drifts
nights and weekends while the real index is closed. This rebuilds the number the real
index shows: today's move vs the previous session's close, frozen outside market hours.
Uses CoinGecko's 7-day hourly sparkline, so it's accurate to within about an hour's move.
Market holidays aren't known; on a holiday it treats the day like a normal session.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN = (9, 30)
CLOSE = (16, 0)


def _is_weekday(d):
    return d.weekday() < 5


def _close_on(day):
    return datetime(day.year, day.month, day.day, *CLOSE, tzinfo=ET)


def _prev_trading_day(day):
    day -= timedelta(days=1)
    while not _is_weekday(day):
        day -= timedelta(days=1)
    return day


def market_open(now_et):
    if not _is_weekday(now_et):
        return False
    t = (now_et.hour, now_et.minute)
    return OPEN <= t < CLOSE


def last_close(now_et):
    """Most recent 4:00 pm ET close at or before now."""
    day = now_et.date()
    if _is_weekday(now_et) and (now_et.hour, now_et.minute) >= CLOSE:
        return _close_on(day)
    return _close_on(_prev_trading_day(day))


def price_at(spark, end_ts, when):
    """Sparkline value nearest to `when` (points are hourly, ending at end_ts)."""
    n = len(spark)
    target = when.timestamp()
    i = round((n - 1) - (end_ts - target) / 3600)
    if i < 0 or i >= n:
        return None
    return spark[i]


def _slice(spark, end_ts, start, stop):
    n = len(spark)
    i0 = max(0, round((n - 1) - (end_ts - start.timestamp()) / 3600))
    i1 = min(n - 1, round((n - 1) - (end_ts - stop.timestamp()) / 3600))
    return spark[i0:i1 + 1] if i1 > i0 else []


def session_change(price, spark, updated_iso, now=None):
    """(percent change, market open?, chart points, absolute change) as the real index reports it:
    today vs the previous close while open; the last session's move when closed."""
    now_et = (now or datetime.now(timezone.utc)).astimezone(ET)
    try:
        end_ts = datetime.fromisoformat(updated_iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        end_ts = now_et.timestamp()
    is_open = market_open(now_et)
    if len(spark) < 30:
        return None, is_open, [], None
    if is_open:
        start = _close_on(_prev_trading_day(now_et.date()))
        stop = now_et
        ref, cur = price_at(spark, end_ts, start), price
    else:
        stop = last_close(now_et)
        start = _close_on(_prev_trading_day(stop.date()))
        ref, cur = price_at(spark, end_ts, start), price_at(spark, end_ts, stop)
    chart = _slice(spark, end_ts, start, stop)
    if not ref or cur is None:
        return None, is_open, chart, None
    return (cur - ref) / ref * 100, is_open, chart, cur - ref
