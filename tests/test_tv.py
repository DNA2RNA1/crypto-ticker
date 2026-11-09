"""TV dashboard: page settings, the data the TV page reads, and who may read it."""

import types

import pytest
from conftest import load_fixture, login, request

import tv
from feeds import parse_weather
from ticker import Config


def test_parse_pages_keeps_order_and_drops_unknown():
    assert tv.parse_pages("Chart, crypto,radar,chart,,weather") == ["chart", "crypto", "weather"]
    assert tv.parse_pages("") == [] and tv.parse_pages(None) == []


def test_screens_for_pages_and_single_mode():
    assert tv.screens_for(["chart", "heatmap"]) == ["coins"]
    assert tv.screens_for(["tracked", "sunmoon", "feargreed"]) == ["weather", "flights", "feargreed"]
    assert set(tv.screens_for([], "single")) == {"coins", "weather", "news", "sports", "flights"}


def test_candles_from_hourly_points():
    pts = [1, 2, 3, 4, 5, 4, 3, 2, 1]
    assert tv.candles_4h(pts) == [[1, 5, 1, 5], [5, 5, 1, 1]]  # each candle closes where the next opens
    assert tv.candles_4h([]) == [] and tv.candles_4h([1]) == []
    assert tv.candles_4h([1, None, "x", 3]) == [[1, 3, 1, 3]]


def test_coin_image_prefers_uploaded_picture(tmp_path, monkeypatch):
    monkeypatch.setattr(tv, "ICON_DIR", str(tmp_path))
    a = {"symbol": "BTC", "image": "https://example.test/btc.png"}
    assert tv.coin_image(a) == "https://example.test/btc.png"
    (tmp_path / "btc.png").write_bytes(b"png")
    assert tv.coin_image(a).startswith("/icons/btc.png?v=")


def test_null_display_waits_without_drawing():
    d = tv.NullDisplay()
    d.show(object(), hold=0.01)
    d.set_brightness(50)
    d.close()


def test_config_for_a_tv(monkeypatch):
    monkeypatch.setenv("DEVICE", "tv")
    monkeypatch.setenv("TV_MODE", "single")
    monkeypatch.setenv("TV_PAGES", "crypto,radar,weather")
    monkeypatch.setenv("TV_SECONDS", "500")
    monkeypatch.setenv("SCREENS", "clock")
    c = Config.from_env()
    assert c.device == "tv" and c.tv_mode == "single" and c.tv_pages == ["crypto", "weather"]
    assert 5 <= c.tv_seconds <= 120
    assert set(c.screens) == {"coins", "weather", "news", "sports", "flights"}  # from the TV, not SCREENS


def test_led_config_ignores_tv_pages(monkeypatch):
    monkeypatch.delenv("DEVICE", raising=False)
    monkeypatch.setenv("SCREENS", "clock")
    monkeypatch.setenv("TV_PAGES", "crypto")
    c = Config.from_env()
    assert c.device == "led" and "clock" in c.screens


def test_weather_days_and_sun_times():
    js = load_fixture("open_meteo.json")
    js["daily"].update(weather_code=[3, 61], sunrise=["2026-11-02T06:58", "2026-11-03T06:59"],
                       sunset=["2026-11-02T17:45", "2026-11-03T17:44"])
    wx = parse_weather(js)
    assert wx["days"] == [{"date": "2026-11-02", "high": 61.4, "low": 45.7, "code": 3},
                          {"date": "2026-11-03", "high": 58.0, "low": 44.1, "code": 61}]
    assert (wx["sunrise"], wx["sunset"]) == ("2026-11-02T06:58", "2026-11-02T17:45")


def test_weather_without_extras_still_parses():
    wx = parse_weather(load_fixture("open_meteo.json"))
    assert "sunrise" not in wx and len(wx["days"]) == 2 and wx["days"][0]["code"] == 0


class _Feed:
    def __init__(self, data=None):
        self.data, self.calls = data, 0

    def update(self):
        self.calls += 1


def _fake_ticker(**cfg):
    c = types.SimpleNamespace(tv_pages=["crypto", "weather"], tv_mode="cycle", tv_seconds=15,
                              tv_flight_popup=True, weather_place="Town", temp_unit="F", currency="usd",
                              flights_track=["AA1"], flights_overhead=True, flights_per_cycle=3)
    c.__dict__.update(cfg)
    flights = _Feed()
    flights.overhead = lambda n: (_ for _ in ()).throw(RuntimeError("boom"))  # a broken feed
    tracker = _Feed()
    tracker.rows = lambda: [{"flight": "AA1", "status": "air"}]
    sports = _Feed()
    sports.games = lambda: []
    return types.SimpleNamespace(
        cfg=c, tz=types.SimpleNamespace(key="America/Chicago"), stale=False,
        assets=[{"symbol": "BTC", "name": "Bitcoin", "price": 1.0, "change_24h": 2.0, "market_cap": 9.0,
                 "image": None, "sparkline": list(range(168))}],
        weather=_Feed({"temp": 70}), fear_greed=_Feed({"value": 50}), news=_Feed([{"title": "Hi", "source": "AP", "at": 1, "link": "x"}]),
        sports=sports, flights=flights, tracker=tracker)


def test_payload_only_feeds_what_the_pages_need():
    t = _fake_ticker()
    p = tv.payload(t, now=100)
    assert p["settings"] == {"mode": "cycle", "pages": ["crypto", "weather"], "seconds": 15, "popup": True}
    assert p["assets"][0]["symbol"] == "BTC" and len(p["assets"][0]["spark"]) == 42
    assert len(p["assets"][0]["candles"]) == 42 and p["weather"] == {"temp": 70}
    assert p["news"] == [] and p["overhead"] == [] and p["tracked"] == [] and p["fear_greed"] is None
    assert t.news.calls == 0 and t.weather.calls == 1 and p["tz"] == "America/Chicago"


def test_payload_survives_a_broken_feed():
    t = _fake_ticker(tv_pages=["flights", "tracked", "news"])
    p = tv.payload(t)
    assert p["overhead"] == [] and p["tracked"] == [{"flight": "AA1", "status": "air"}]
    assert p["news"] == [{"title": "Hi", "source": "AP", "at": 1}]  # only what the page shows


# --- phone page and HTTP ------------------------------------------------------
def test_tv_settings_save_and_validate(app):
    app.save({"tv_mode": "single", "tv_pages": "crypto,chart", "tv_seconds": 20, "tv_flight_popup": "off"})
    text = open(app.env_path).read()
    assert "TV_MODE=single" in text and "TV_PAGES=crypto,chart" in text and "TV_SECONDS=20" in text
    s = app.settings()
    assert s["tv_mode"] == "single" and s["tv_pages"] == ["crypto", "chart"] and s["device"] == "led"
    for bad in ({"tv_mode": "grid"}, {"tv_pages": "crypto,radar"}, {"tv_pages": "crypto,crypto"},
                {"tv_seconds": 2}, {"tv_flight_popup": "maybe"}):
        with pytest.raises(ValueError):
            app.save(bad)
    app.save({"tv_pages": ""})  # every page switched off is allowed
    assert app.settings()["tv_pages"] == []


def test_device_comes_from_the_installer(app):
    with open(app.env_path, "a") as fh:
        fh.write("DEVICE=tv\n")
    assert app.settings()["device"] == "tv"
    assert app.settings()["tv_pages"] == list(tv.DEFAULT_TV_PAGES)


def _get_html(server, path):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    return r.status, r.read(), r.getheader("Location")


def test_tv_page_open_to_this_pi_without_pin(server):
    # the test client connects from 127.0.0.1, like the TV's own browser
    status, body, _ = _get_html(server, "/tv")
    assert status == 200 and b"/api/tv" in body
    assert request(server, "GET", "/api/tv")[0] == 503  # no ticker running in this test


def test_tv_page_needs_login_from_elsewhere(server, monkeypatch):
    monkeypatch.setattr(server.RequestHandlerClass, "_local", lambda self: False)
    assert _get_html(server, "/tv")[0] == 302
    assert request(server, "GET", "/api/tv")[0] == 401
    auth = login(server)
    assert request(server, "GET", "/api/tv", headers=auth)[0] == 503


def test_payload_extras(tmp_path, monkeypatch):
    monkeypatch.setattr(tv, "AIRLINE_DIR", str(tmp_path))
    (tmp_path / "UAL.png").write_bytes(b"png")
    (tmp_path / "notes.txt").write_text("x")
    t = _fake_ticker(tv_pages=["flights"])
    t.tz = None  # no TIMEZONE set: the TV uses the Pi's clock
    p = tv.payload(t)
    assert p["tz"] is None and p["build"] > 0
    assert list(p["airline_icons"]) == ["UAL"] and p["airline_icons"]["UAL"].startswith("/icons/airlines/UAL.png?v=")


def test_feed_starts_one_refresh_when_asked_twice(monkeypatch):
    import threading
    from feeds import Feed
    started = []
    monkeypatch.setattr(threading, "Thread", lambda target, daemon: types.SimpleNamespace(start=lambda: started.append(1)))
    f = Feed()
    f.update(now=100)
    f.update(now=100)
    assert started == [1]


def test_tv_has_no_transitions(monkeypatch):
    monkeypatch.setenv("DEVICE", "tv")
    monkeypatch.setenv("TRANSITION", "scroll")
    from ticker import Config
    assert Config.from_env().transition == "none"


def test_airline_pictures_served_for_the_tv(server, monkeypatch, tmp_path):
    import webui
    monkeypatch.setattr(webui, "AIRLINE_DIR", str(tmp_path))
    from PIL import Image
    Image.new("RGB", (4, 4)).save(tmp_path / "DAL.png")
    status, body, _ = _get_html(server, "/icons/airlines/DAL.png")
    assert status == 200 and body[:4] == b"\x89PNG"
    assert _get_html(server, "/icons/airlines/dal.png")[0] != 200
    assert _get_html(server, "/icons/airlines/../settings.env")[0] != 200
