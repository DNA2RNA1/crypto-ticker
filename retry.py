"""Retry with exponential backoff, used for every price-API request.

Two levels:
  * ``retry_call`` retries a single request a couple of times within a few seconds
    (a dropped packet or a brief Wi-Fi blip), so one hiccup doesn't mark prices stale.
  * ``backoff_delay`` spaces out whole refresh attempts after repeated failures
    (Wi-Fi down for an hour, CoinGecko throttling), so the ticker doesn't hammer the
    API or flood the log.
"""

import logging
import random
import time

log = logging.getLogger("ticker.retry")


class Retryable(Exception):
    """Raise (or subclass) this for errors worth trying again."""


def backoff_delay(attempt, base=1.0, cap=60.0, jitter=0.2, rng=None):
    """Delay before retry number ``attempt`` (1 = first retry): base * 2**(attempt-1),
    capped, with +/- ``jitter`` fraction of randomness so many devices don't sync up."""
    if attempt < 1:
        return 0.0
    delay = min(cap, base * (2 ** (attempt - 1)))
    if jitter:
        delay *= 1 + jitter * (2 * (rng or random.random)() - 1)
    return max(0.0, min(cap, delay))


def retry_call(fn, attempts=3, base=1.0, cap=8.0, retry_on=(Retryable,), sleep=time.sleep,
               what="request"):
    """Call ``fn()``; on an exception listed in ``retry_on`` wait and try again, up to
    ``attempts`` tries in total. Other exceptions propagate immediately. The last error is
    re-raised if every try fails."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            if attempt == attempts:
                raise
            delay = backoff_delay(attempt, base, cap)
            log.info("%s failed (%s); retry %d/%d in %.1fs", what, exc, attempt, attempts - 1, delay)
            sleep(delay)
