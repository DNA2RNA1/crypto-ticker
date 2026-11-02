"""A small request cache for API responses.

* Fresh hits (younger than the TTL) are served from memory without a network call,
  so a settings reload or repeated search doesn't spend the monthly CoinGecko budget.
* Entries marked ``persist`` are also kept on disk, so after a reboot with no Wi-Fi
  the ticker can still show the last good prices (flagged as stale) instead of NO DATA.

Disk writes are rate-limited (``min_write_interval``) to spare the SD card.
"""

import json
import logging
import os
import threading
import time

log = logging.getLogger("ticker.cache")


def make_key(*parts, **params):
    """Stable string key from a path and query parameters."""
    items = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return "/".join(str(p) for p in parts) + ("?" + items if items else "")


class RequestCache:
    def __init__(self, path=None, max_entries=64, min_write_interval=300, clock=time.time):
        self.path = path
        self.max_entries = max_entries
        self.min_write_interval = min_write_interval
        self.clock = clock
        self._lock = threading.Lock()
        self._mem = {}           # key -> (stored_at, value)
        self._persist = set()    # keys that also go to disk
        self._last_write = 0.0
        self._dirty = False
        self._load()

    # --- public API -------------------------------------------------------
    def get(self, key, max_age):
        """Value stored less than ``max_age`` seconds ago, else None."""
        with self._lock:
            hit = self._mem.get(key)
        if hit and self.clock() - hit[0] < max_age:
            return hit[1]
        return None

    def get_any(self, key):
        """(stored_at, value) regardless of age, or None. Used as a fallback when offline."""
        with self._lock:
            return self._mem.get(key)

    def put(self, key, value, persist=False):
        now = self.clock()
        with self._lock:
            self._mem[key] = (now, value)
            if persist:
                self._persist.add(key)
                self._dirty = True
            if len(self._mem) > self.max_entries:  # drop the oldest entries
                for old in sorted(self._mem, key=lambda k: self._mem[k][0])[: len(self._mem) - self.max_entries]:
                    self._mem.pop(old, None)
                    self._persist.discard(old)
        if persist:
            self.flush()

    def flush(self, force=False):
        """Write persistent entries to disk (at most every ``min_write_interval``)."""
        if not self.path:
            return
        now = self.clock()
        with self._lock:
            if not self._dirty or (not force and now - self._last_write < self.min_write_interval):
                return
            data = {k: {"t": self._mem[k][0], "v": self._mem[k][1]}
                    for k in self._persist if k in self._mem}
            self._dirty = False
            self._last_write = now
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not save request cache %s: %s", self.path, exc)

    # --- internals --------------------------------------------------------
    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path) as fh:
                data = json.load(fh)
            for key, entry in data.items():
                self._mem[key] = (float(entry["t"]), entry["v"])
                self._persist.add(key)
            log.info("loaded %d cached response(s) from %s", len(data), self.path)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("ignoring unreadable request cache %s: %s", self.path, exc)
            self._mem.clear()
            self._persist.clear()
