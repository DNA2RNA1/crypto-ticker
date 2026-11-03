"""Weather screen: Open-Meteo parsing, rendering, and skipping when there's nothing to show."""

import copy
import json
from datetime import datetime

import pytest
import requests
from conftest import FakeResponse, load_fixture

from feeds import Weather, parse_weather
from render import Renderer
from sample import sample_weather
from test_ticker import FakeDisplay
from ticker import Config, Ticker

NOW = datetime(2026, 11, 2, 14, 15)


@pytest.fixture
def meteo():
    return load_fixture("open_meteo.json")


def test_parse_normal(meteo):
    wx = parse_weather(meteo)
    assert (wx["temp"], wx["code"], wx["is_day"]) == (61.2, 2, True)
    assert (wx["high"], wx["low"]) == (61.4, 45.7)
    assert len(wx["hourly"]) == 13 and wx["hourly"][0] == 61.2 and wx["hourly"][-1] == 48.4  # 14:00 .. 02:00


@pytest.mark.parametrize("drop", ["daily", "hourly"])
def test_parse_missing_extras(meteo, drop):
    del meteo[drop]
    wx = parse_weather(meteo)
    assert wx["temp"] == 61.2
    assert (wx["high"] is None) == (drop == "daily") and (wx["hourly"] == []) == (drop == "hourly")


@pytest.mark.parametrize("mutate", [
    lambda m: m["current"].pop("temperature_2m"),
    lambda m: m.pop("current"),
    lambda m: m["current"].update(temperature_2m="hot"),
])
def test_parse_malformed_raises(meteo, mutate):
    mutate(meteo)
    with pytest.raises((KeyError, ValueError, TypeError)):
        parse_weather(meteo)


def test_parse_odd_extras_are_dropped(meteo):
    meteo["hourly"]["temperature_2m"][16] = None
    meteo["daily"] = {"temperature_2m_max": "n/a"}
    wx = parse_weather(meteo)
    assert wx["hourly"] == [] and wx["high"] is None


def run_feed(feed, response):
    feed.session.get = lambda *a, **k: response() if callable(response) else response
    feed._busy = True
    feed._run()
    return feed.data


def test_feed_requests_units_and_keeps_last_good(meteo):
    w = Weather(40.0, -75.0, "celsius")
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(params)
        return FakeResponse(200, meteo)

    w.session.get = get
    w._busy = True
    w._run()
    assert w.data["high"] == 61.4
    assert calls[0]["temperature_unit"] == "celsius" and "temperature_2m_max" in calls[0]["daily"]
    good = copy.deepcopy(w.data)

    def offline():
        raise requests.exceptions.ConnectionError("Failed to resolve 'api.open-meteo.com'")

    assert run_feed(w, offline) == good                              # Wi-Fi loss: keep last good
    assert run_feed(w, FakeResponse(200, text="<html>oops")) == good  # malformed
    assert run_feed(w, FakeResponse(200, {"error": True})) == good    # wrong shape


def test_no_location_means_no_data():
    w = Weather(None, None)
    w.session.get = lambda *a, **k: pytest.fail("must not fetch without a location")
    w._busy = True
    w._run()
    assert w.data is None


@pytest.mark.parametrize("wx", [
    sample_weather(),
    dict(sample_weather(71), temp=-12.4, high=-3, low=-18),
    dict(sample_weather(3), temp=104.6, high=109, low=88),
    dict(sample_weather(95), high=None, low=None, hourly=[]),
    dict(sample_weather(0, is_day=False), hourly=[50.0] * 13),  # flat line
])
def test_render_fits(wx):
    img = Renderer().weather(wx, "F", NOW)
    assert img.size == (64, 32) and img.getbbox() is not None


def ticker_with(data):
    cfg = Config()
    cfg.screens = ["weather"]
    t = Ticker(cfg, FakeDisplay())
    t.weather.data, t.weather.next_fetch = data, float("inf")
    return t


def test_screen_shown_with_data():
    assert len(ticker_with(sample_weather()).screens()) == 1


def test_screen_skipped_without_data():
    t = ticker_with(None)
    assert t.screens() == []
    t.assets, t.next_fetch = [], float("inf")
    t.last_error = None
    t.step()  # no crash, nothing shown
    assert t.display.frames == []


def test_weather_screen_respects_unit():
    t = ticker_with(sample_weather())
    t.cfg.temp_unit = "C"
    seen = []
    t.renderer.weather = lambda wx, unit, now: seen.append(unit) or Renderer().weather(wx, unit, now)
    t.screens()[0]()
    assert seen == ["C"]


def test_screen_in_settings(monkeypatch):
    monkeypatch.setenv("SCREENS", "clock,weather,coins")
    assert Config.from_env().screens == ["clock", "weather", "coins"]
    monkeypatch.delenv("SCREENS")
    assert "weather" not in Config.from_env().screens  # opt-in


def test_fixture_is_valid_json():
    json.dumps(load_fixture("open_meteo.json"))


def test_place_label_city_and_state():
    from render import place_label
    assert place_label("Edinburg, Texas, US") == "EDINBURG, TX"
    assert place_label("Santa Fe, New Mexico, US") == "SANTA FE, NM"
    assert place_label("Paris, Île-de-France, France") == "PARIS, ÎLE-DE-FRANCE"
    assert place_label("Edinburg") == "EDINBURG"
    assert place_label("") == ""


def test_clock_place_fits_and_drops_state_when_long():
    from datetime import datetime
    from render import Renderer
    r = Renderer()
    for place in ("Edinburg, Texas, US", "Rancho Santa Margarita, California, US"):
        img = r.clock(datetime(2026, 11, 2, 9, 5), None, "F", place)
        assert img.size == (64, 32)
