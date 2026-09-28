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


def session_change(price, spark, updated_iso, now=None):
    """Percent change the way the real index reports it, plus whether it's open."""
    now_et = (now or datetime.now(timezone.utc)).astimezone(ET)
    try:
        end_ts = datetime.fromisoformat(updated_iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        end_ts = now_et.timestamp()
    if len(spark) < 30:
        return None, market_open(now_et)
    if market_open(now_et):
        ref = price_at(spark, end_ts, _close_on(_prev_trading_day(now_et.date())))
        cur = price
    else:
        close = last_close(now_et)
        cur = price_at(spark, end_ts, close)
        ref = price_at(spark, end_ts, _close_on(_prev_trading_day(close.date())))
    if not ref or cur is None:
        return None, market_open(now_et)
    return (cur - ref) / ref * 100, market_open(now_et)
