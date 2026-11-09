"""Candle chart style: candles built from the 7-day hourly prices, drawn on the chart layout,
switchable from the phone page."""

import pytest

from render import DOWN, UP, Renderer
from sample import sample_assets
from ticker import Config


def test_candles_from_hourly_prices():
    pts = [float(i) for i in range(169)]          # steadily rising week
    cs = Renderer.candles_from(pts, 16)
    assert len(cs) == 16
    assert all(c[3] > c[0] for c in cs)            # every candle closes higher
    assert all(cs[i][3] == cs[i + 1][0] for i in range(15))  # each opens at the last close
    assert cs[0][0] == 0 and cs[-1][3] == 168
    o, h, l, c = Renderer.candles_from([5, 9, 1, 4, 6], 1)[0]
    assert (o, h, l, c) == (5, 9, 1, 6)


@pytest.mark.parametrize("pts", [[], [1.0], [1.0] * 10])
def test_too_little_history_draws_nothing(pts):
    assert Renderer.candles_from(pts, 16) == []
    r = Renderer(64, 32, layout="chart", chart_style="candles")
    a = dict(sample_assets(["btc"])[0], sparkline=pts)
    assert r.asset(a).size == (64, 32)


@pytest.mark.parametrize("size", [(64, 32), (64, 64), (128, 32)])
def test_candle_screen_has_up_and_down_candles(size):
    r = Renderer(*size, layout="chart", chart_style="candles")
    img = r.asset(sample_assets(["btc"])[0])
    colors = {img.getpixel((x, y)) for x in range(size[0]) for y in range(size[1])}
    assert UP in colors and DOWN in colors


def test_line_style_unchanged_by_default():
    a = sample_assets(["eth"])[0]
    assert Renderer(64, 32, layout="chart").asset(a).tobytes() == \
        Renderer(64, 32, layout="chart", chart_style="line").asset(a).tobytes()
    assert Renderer(64, 32, chart_style="bogus").chart_style == "line"


def test_setting(monkeypatch):
    assert Config.from_env().chart_style == "line"
    monkeypatch.setenv("CHART_STYLE", "candles")
    assert Config.from_env().chart_style == "candles"
    monkeypatch.setenv("CHART_STYLE", "bars")
    assert Config.from_env().chart_style == "line"


def test_phone_page_saves_chart_style(app):
    assert app.save({"chart_style": "candles"})["CHART_STYLE"] == "candles"
    assert app.settings()["chart_style"] == "candles"
    with pytest.raises(ValueError):
        app.save({"chart_style": "candles\nWEB_PIN="})
