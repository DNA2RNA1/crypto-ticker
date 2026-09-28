"""Tracks CoinGecko calls per month and picks a refresh interval that fits the budget.

CoinGecko's free Demo plan allows 10,000 calls a month. In "auto" mode the
ticker refreshes as often as once a minute, slowing down only when the month's
pace would overshoot 90% of the budget. It learns how many hours a day the
ticker actually runs, so a Pi that's off overnight gets faster updates.
"""

import json
import os
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "cache", "usage.json")

MIN_INTERVAL = 60      # CoinGecko's free data only updates once a minute
MAX_INTERVAL = 1800
SAFETY = 0.90          # aim to use at most 90% of the budget


def _month_bounds(now):
    d = datetime.fromtimestamp(now)
    start = datetime(d.year, d.month, 1)
    end = datetime(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return start.timestamp(), end.timestamp(), d.strftime("%Y-%m")


class Usage:
    def __init__(self, budget=10000, path=PATH):
        self.budget = budget
        self.path = path
        self._lock = threading.Lock()
        self._last_tick = None
        self._last_save = 0.0
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        return self._rollover(data, time.time())

    def _rollover(self, data, now):
        month = _month_bounds(now)[2]
        if data.get("month") != month:
            data = {"month": month, "calls": 0, "run_seconds": 0.0}
        return data

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh)
        os.replace(tmp, self.path)

    def count_call(self, now=None):
        with self._lock:
            self.data = self._rollover(self.data, now or time.time())
            self.data["calls"] += 1
            self._save()

    def tick(self, now=None):
        """Record running time. Gaps longer than 10 minutes count as 'off'."""
        now = now or time.time()
        with self._lock:
            self.data = self._rollover(self.data, now)
            if self._last_tick is not None:
                gap = now - self._last_tick
                if 0 < gap <= 600:
                    self.data["run_seconds"] += gap
            self._last_tick = now
            if now - self._last_save >= 60:  # spare the SD card
                self._save()
                self._last_save = now

    @property
    def calls(self):
        return self.data.get("calls", 0)

    def auto_interval(self, now=None):
        """Fastest interval (seconds) that keeps the month under budget."""
        now = now or time.time()
        start, end, _ = _month_bounds(now)
        elapsed = max(now - start, 86400)  # judge on-time over at least a day
        on_fraction = min(1.0, max(0.25, self.data.get("run_seconds", 0) / elapsed))
        expected_runtime = (end - now) * on_fraction
        remaining = self.budget * SAFETY - self.calls
        if remaining <= 0:
            return MAX_INTERVAL
        return int(min(MAX_INTERVAL, max(MIN_INTERVAL, expected_runtime / remaining)))

    def summary(self, interval):
        return {"calls": self.calls, "budget": self.budget, "month": self.data["month"],
                "interval": int(interval)}
