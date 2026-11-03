"""Gold & silver screen, and the Yahoo host fallback shared with the indices."""

import pytest

import feeds
from feeds import Indices, Metals
from render import DOWN, UP, Renderer


def chart_json(price, prev, closes=(1, 2, 3)):
    return {"chart": {"result": [{"meta": {"regularMarketPrice": price, "chartPreviousClose": prev},
                                  "indicators": {"quote": [{"close": list(closes)}]}}]}}


class Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data
        self.ok = status == 200

    def raise_for_status(self):
        if self.status_code != 200:
            raise feeds.requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._data


class FakeSession:
    """Answers by (host, symbol); anything not listed gets the default status."""
    def __init__(self, answers, default=404):
        self.answers, self.default, self.calls = answers, default, []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        host = url.split("//")[1].split(".")[0]
        symbol = url.rsplit("/", 1)[1]
        self.calls.append((host, symbol))
        status, data = self.answers.get((host, symbol), (self.default, None))
        return Resp(status, data)


def test_dow_uses_query2_when_query1_is_rate_limited():
    f = Indices([("^DJI", "DOW")])
    f.session = FakeSession({("query2", "^DJI"): (200, chart_json(47000.0, 47500.0)),
                             ("query1", "^DJI"): (429, None)})
    (row,) = f.fetch()
    assert row["label"] == "DOW" and row["price"] == 47000.0 and row["change"] < 0
    assert f.session.calls[0] == ("query2", "^DJI")


def test_falls_back_to_query1_when_query2_fails():
    f = Indices([("^DJI", "DOW")])
    f.session = FakeSession({("query1", "^DJI"): (200, chart_json(47000.0, 46000.0))}, default=500)
    assert f.fetch()[0]["change"] > 0


def test_both_hosts_failing_raises():
    f = Indices([("^DJI", "DOW")])
    f.session = FakeSession({}, default=429)
    with pytest.raises(Exception):
        f.fetch()


def test_metals_prefers_spot_then_futures():
    m = Metals()
    m.session = FakeSession({("query2", "XAUUSD=X"): (200, chart_json(4012.5, 4000.0)),
                             ("query2", "SI=F"): (200, chart_json(48.2, 49.0))})
    rows = m.fetch()
    assert [(r["label"], r["symbol"]) for r in rows] == [("GOLD", "XAUUSD=X"), ("SILVER", "SI=F")]


def test_metals_one_missing_still_shows_the_other():
    m = Metals()
    m.session = FakeSession({("query1", "GC=F"): (200, chart_json(4012.5, 4000.0))})
    assert [r["label"] for r in m.fetch()] == ["GOLD"]


def test_metals_all_missing_raises():
    m = Metals()
    m.session = FakeSession({})
    with pytest.raises(ValueError):
        m.fetch()


@pytest.mark.parametrize("rows", [
    [{"label": "GOLD", "price": 4012.5, "change": 0.4, "intraday": [1, 2, 3]},
     {"label": "SILVER", "price": 48.21, "change": -1.2, "intraday": [3, 2, 1]}],
    [{"label": "GOLD", "price": 12345.67, "change": 0.4, "intraday": []}],
])
def test_metals_screen_draws_whole_prices_in_trend_colors(rows):
    r = Renderer()
    font = r.fonts["5x8"]
    drawn = []
    real = font.draw
    font.draw = lambda img, x, b, t, c: (drawn.append((x, t)), real(img, x, b, t, c))[1]
    img = r.metals(rows)
    assert img.size == (64, 32)
    texts = [t for _, t in drawn]
    assert any(t.startswith(("$4,01", "$12,34")) for t in texts)  # whole price, no K
    assert not any("K" in t for t in texts)
    for x, t in drawn:
        assert x >= 0 and x + font.width(t) <= 64
    colors = {img.getpixel((x, y)) for x in range(64) for y in range(32)}
    assert UP in colors
    if len(rows) == 2:
        assert DOWN in colors


def test_metals_is_a_selectable_screen():
    from ticker import SCREEN_NAMES
    from webui import SCREENS
    assert "metals" in SCREEN_NAMES and "metals" in SCREENS
