"""Backoff math, the request cache and log rotation."""

import logging
import os

from cache import RequestCache
from logsetup import setup_logging
from retry import Retryable, backoff_delay, retry_call


def test_backoff_doubles_and_caps():
    assert [backoff_delay(n, base=1, cap=8, jitter=0) for n in range(0, 7)] == [0, 1, 2, 4, 8, 8, 8]


def test_backoff_jitter_bounds():
    assert backoff_delay(3, base=10, cap=100, jitter=0.2, rng=lambda: 0.0) == 32
    assert backoff_delay(3, base=10, cap=100, jitter=0.2, rng=lambda: 1.0) == 48


def test_retry_call_gives_up_and_reraises():
    calls, sleeps = [], []

    def boom():
        calls.append(1)
        raise Retryable("nope")

    try:
        retry_call(boom, attempts=3, sleep=sleeps.append)
    except Retryable:
        pass
    assert len(calls) == 3 and len(sleeps) == 2


def test_retry_call_does_not_retry_other_errors():
    calls = []

    def boom():
        calls.append(1)
        raise KeyError("x")

    try:
        retry_call(boom, sleep=lambda s: None)
    except KeyError:
        pass
    assert len(calls) == 1


def test_cache_ttl_and_persistence(tmp_path):
    now = [1000.0]
    path = str(tmp_path / "c.json")
    c = RequestCache(path, clock=lambda: now[0], min_write_interval=300)
    c.put("a", [1], persist=True)
    c.put("b", [2])  # memory only
    assert c.get("a", 60) == [1]
    now[0] += 61
    assert c.get("a", 60) is None and c.get_any("a") == (1000.0, [1])
    c2 = RequestCache(path)
    assert c2.get_any("a") == (1000.0, [1]) and c2.get_any("b") is None


def test_cache_limits_disk_writes(tmp_path):
    now = [1000.0]
    path = str(tmp_path / "c.json")
    c = RequestCache(path, clock=lambda: now[0], min_write_interval=300)
    c.put("a", 1, persist=True)
    mtime = os.path.getmtime(path)
    os.utime(path, (mtime - 50, mtime - 50))
    now[0] += 10
    c.put("a", 2, persist=True)  # too soon: not written
    assert RequestCache(path).get_any("a")[1] == 1
    c.flush(force=True)
    assert RequestCache(path).get_any("a")[1] == 2


def test_cache_ignores_corrupt_file(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{corrupt")
    assert RequestCache(str(path)).get_any("x") is None


def test_cache_evicts_oldest():
    now = [0.0]
    c = RequestCache(max_entries=2, clock=lambda: now[0])
    for k in "abc":
        now[0] += 1
        c.put(k, k)
    assert c.get_any("a") is None and c.get_any("c")[1] == "c"


def test_log_file_rotates_and_stays_small(tmp_path):
    log_file = str(tmp_path / "logs" / "ticker.log")
    setup_logging(level="INFO", log_file=log_file, max_kb=16, backups=2, console=False)
    log = logging.getLogger("ticker.test")
    for i in range(2000):
        log.info("price line %05d %s", i, "x" * 60)
    files = sorted(os.listdir(tmp_path / "logs"))
    assert files == ["ticker.log", "ticker.log.1", "ticker.log.2"]
    total = sum(os.path.getsize(tmp_path / "logs" / f) for f in files)
    assert total <= 3 * 16 * 1024 + 200
    for h in logging.getLogger().handlers:
        h.close()
    logging.getLogger().handlers.clear()
