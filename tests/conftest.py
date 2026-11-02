"""Shared fixtures: canned CoinGecko responses and a fake requests layer.

No test touches the network, the LED panel or the repo's own cache/ and logs/ folders.
"""

import json
import os
import sys

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


class FakeResponse:
    def __init__(self, status=200, data=None, text=None, headers=None):
        self.status_code = status
        self._data = data
        self.text = text if text is not None else json.dumps(data)
        self.headers = headers or {}

    def json(self):
        if self._data is None:
            return json.loads(self.text)  # raises ValueError for malformed bodies
        return self._data


class FakeHTTP:
    """Stands in for requests.Session.get. Queue responses or exceptions with add();
    once the queue is empty the last item repeats. Records every call."""

    def __init__(self):
        self.queue = []
        self.calls = []

    def add(self, *items):
        self.queue.extend(items)
        return self

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def markets():
    return load_fixture("markets.json")


@pytest.fixture
def http():
    return FakeHTTP()


@pytest.fixture
def ok(markets):
    return FakeResponse(200, markets)


# common failures, as requests would raise or return them
def timeout():
    return requests.exceptions.ReadTimeout("HTTPSConnectionPool: Read timed out. (read timeout=10)")


def wifi_down():
    return requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='api.coingecko.com'): Max retries exceeded "
        "(Caused by NameResolutionError: Failed to resolve 'api.coingecko.com')")


def rate_limited(retry_after="90"):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return FakeResponse(429, {"status": {"error_code": 429, "error_message": "You've exceeded the Rate Limit"}},
                        headers=headers)


def malformed():
    return FakeResponse(200, text='[{"id": "bitcoin", "current_price": 1092')  # cut off mid-body


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Keep caches and usage counters out of the repo during tests."""
    import ticker
    import usage
    monkeypatch.setattr(ticker, "REQUEST_CACHE", str(tmp_path / "requests.json"))
    monkeypatch.setattr(ticker, "Usage", lambda budget: usage.Usage(budget, path=str(tmp_path / "usage.json")))
    yield
