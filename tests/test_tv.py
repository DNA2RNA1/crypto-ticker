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
    assert p["settings"] == {"mode": "cycle", "pages": ["crypto", "weather"], "seconds": 15, "popup": True,
                             "odds": False, "arcade": "led"}
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


def test_arcade_scenes_for_the_tv():
    import io
    from PIL import Image
    scenes = tv.arcade_list()
    assert len(scenes) >= 6 and all(s["frames"] > 0 and s["fps"] > 0 for s in scenes)
    sheet = Image.open(io.BytesIO(tv.arcade_sheet(0)))
    assert sheet.size == (64, 32 * scenes[0]["frames"])
    with pytest.raises(IndexError):
        tv.arcade_sheet(99)
    assert "arcade" in tv.DEFAULT_TV_PAGES and tv.screens_for(["arcade"]) == []  # needs no feed


def test_arcade_served_to_the_tv(server):
    status, body, _ = _get_html(server, "/api/tv/arcade/0.png")
    assert status == 200 and body[:4] == b"\x89PNG"
    assert request(server, "GET", "/api/tv/arcade")[1]["scenes"][0]["name"]
    assert _get_html(server, "/api/tv/arcade/42.png")[0] == 404


def test_arcade_needs_login_from_elsewhere(server, monkeypatch):
    monkeypatch.setattr(server.RequestHandlerClass, "_local", lambda self: False)
    assert request(server, "GET", "/api/tv/arcade")[0] == 401


def test_betting_lines_list_every_upcoming_game_with_a_line():
    import sports as sp
    s = sp.Sports.__new__(sp.Sports)
    import threading
    s._lock = threading.Lock()
    lg = sp.BY_KEY["nfl"]
    s.active = lambda now: [lg]
    s.followed = lambda lg, games: (set(), {"7"})
    line = {"spread": {"away": "+3.5", "home": "-3.5"}, "total": "47.5", "ml": None}

    def game(gid, state, start, odds, away="1", home="2"):
        return {"id": gid, "league": "nfl", "state": state, "start": start, "odds": odds,
                "away": {"id": away, "abbr": "A" + gid}, "home": {"id": home, "abbr": "H" + gid}}
    s.boards = {"nfl": (0, [game("3", "pre", 3000, line), game("1", "pre", 2000, line, away="7"),
                            game("2", "pre", 2500, None), game("4", "in", 1000, None), game("5", "pre", 100, line)])}
    out = s.lines(now=1000)
    assert [g["id"] for g in out] == ["1", "3"]  # soonest first; no line, live or long past left out
    assert out[0]["fav"] == {"away": True, "home": False}


def test_payload_sends_lines_only_when_asked():
    t = _fake_ticker(tv_pages=["scores"], sports_odds=False)
    g = {"league": "nfl", "state": "pre", "start": 1, "detail": "", "odds": {"total": "40"},
         "away": {"abbr": "A"}, "home": {"abbr": "B"}, "fav": {"away": False, "home": True}}
    t.sports.games = lambda: [g]
    t.sports.lines = lambda: [g]
    p = tv.payload(t)
    assert p["games"][0]["odds"] is None and p["lines"] == [] and p["settings"]["odds"] is False
    t.cfg.sports_odds = True
    p = tv.payload(t)
    assert p["games"][0]["odds"] == {"total": "40"} and p["lines"]  # the dashboard fills in from these
    t.cfg.sports_odds = False
    t.cfg.tv_pages = ["lines"]
    p = tv.payload(t)
    assert p["lines"][0]["odds"] == {"total": "40"} and p["lines"][0]["fav"]["home"] is True


def test_arcade_look_setting(app, monkeypatch):
    app.save({"tv_arcade_style": "pixels"})
    assert "TV_ARCADE_STYLE=pixels" in open(app.env_path).read() and app.settings()["tv_arcade_style"] == "pixels"
    with pytest.raises(ValueError):
        app.save({"tv_arcade_style": "blurry"})
    monkeypatch.setenv("TV_ARCADE_STYLE", "PIXELS")
    assert Config.from_env().tv_arcade_style == "pixels"
    monkeypatch.setenv("TV_ARCADE_STYLE", "nonsense")
    assert Config.from_env().tv_arcade_style == "led"


# --- flight map --------------------------------------------------------------------------
def test_map_pan_and_reach():
    import types as t
    assert tv.parse_pan("3.5,-2") == (3.5, -2.0) and tv.parse_pan("") == (0.0, 0.0)
    assert tv.parse_pan("nan,1") == (0.0, 0.0) and tv.parse_pan("500,-500") == (100, -100)
    c = t.SimpleNamespace(device="tv", tv_pages=["flightmap"], flights_radius=10, tv_map_radius=20, tv_map_pan=(0, 0))
    assert tv.flights_fetch_radius(c) == 46            # fills the screen at 20 mi
    c.tv_map_radius = 100
    assert tv.flights_fetch_radius(c) == 230
    c.tv_pages = ["flights"]
    assert tv.flights_fetch_radius(c) == 10            # map off: just the Flights setting
    c.device, c.tv_pages = "led", ["flightmap"]
    assert tv.flights_fetch_radius(c) == 10            # never on an LED panel


def test_map_settings_from_env(monkeypatch):
    monkeypatch.setenv("DEVICE", "tv")
    monkeypatch.setenv("TV_PAGES", "flightmap")
    monkeypatch.setenv("TV_MAP_RADIUS", "50")
    monkeypatch.setenv("TV_MAP_STYLE", "Light")
    monkeypatch.setenv("TV_MAP_PAN", "5,-2.5")
    c = Config.from_env()
    assert (c.tv_map_radius, c.tv_map_style, c.tv_map_pan) == (50, "light", (5.0, -2.5))
    assert "flights" in c.screens
    monkeypatch.setenv("TV_MAP_RADIUS", "33")
    assert Config.from_env().tv_map_radius == 20       # not a choice: the default


def test_overhead_can_stay_within_the_flights_radius():
    import flights as fl
    f = fl.Flights.__new__(fl.Flights)
    f.data = [{"callsign": "A", "dist_mi": 3.0}, {"callsign": "B", "dist_mi": 30.0}]
    f.fetched = 1000
    f.route = lambda cs, now=None: None
    assert [p["callsign"] for p in f.overhead(5, now=1000)] == ["A", "B"]
    assert [p["callsign"] for p in f.overhead(5, now=1000, max_mi=10)] == ["A"]


def test_flight_map_payload():
    t = _fake_ticker(tv_pages=["flightmap"], weather_lat=26.3, weather_lon=-98.16, tv_map_radius=20,
                     tv_map_style="dark", tv_map_pan=(1.0, 0.0), flights_radius=10, flights_overhead=False)
    t.flights.fetched = 990
    t.flights.overhead = lambda n, now=None, max_mi=None: [
        {"hex": "abc", "callsign": "AAL1", "lat": 26.4, "lon": -98.1, "track": 90, "alt_ft": 5000,
         "speed_kt": 200, "vrate_fpm": -800, "dist_mi": 7.0, "reg": "", "type": "A321",
         "route": {"orig": "DFW", "dest": "MFE", "orig_pos": (32.9, -97.0), "dest_pos": (26.2, -98.2), "x": 1}}]
    m = tv.payload(t, now=1000)["map"]
    assert m["radius"] == 20 and m["pan"] == [1.0, 0.0] and m["style"] == "dark" and m["fetched"] == 990
    assert m["planes"][0]["hex"] == "abc" and m["planes"][0]["route"] == {
        "orig": "DFW", "dest": "MFE", "orig_pos": (32.9, -97.0), "dest_pos": (26.2, -98.2)}
    assert t.flights.calls == 1                        # the map fetches planes even with Flights off
    t.cfg.weather_lat = None
    assert tv.payload(t)["map"] is None                # no location set: no map


def test_map_settings_save(app):
    app.save({"tv_map_radius": "50", "tv_map_style": "light", "tv_map_pan": "5,-2.5"})
    s = app.settings()
    assert (s["tv_map_radius"], s["tv_map_style"], s["tv_map_pan"]) == (50, "light", [5.0, -2.5])
    for bad in ({"tv_map_radius": "33"}, {"tv_map_style": "neon"}, {"tv_map_pan": "500,0"}, {"tv_map_pan": "x"}):
        with pytest.raises(ValueError):
            app.save(bad)


def test_map_home():
    import types as t
    assert tv.parse_home("26.301234567,-98.16") == (26.30123, -98.16) and tv.parse_home("") is None
    assert tv.parse_home("95,0") is None and tv.parse_home("main st") is None
    c = t.SimpleNamespace(device="tv", tv_pages=["flightmap"], flights_radius=10, tv_map_radius=1,
                          tv_map_pan=(0, 0), tv_map_home=None, weather_lat=26.3017, weather_lon=-98.1633)
    assert tv.map_home(c) == (26.3017, -98.1633)            # the weather location by default
    assert tv.flights_fetch_radius(c) == 10                  # 1-mile zoom: the Flights radius is plenty
    c.tv_map_home = (26.3017, -98.0633)                      # about 6 mi east of the weather location
    assert tv.map_home(c) == (26.3017, -98.0633)
    assert tv.flights_fetch_radius(c) == 10 and tv.flights_fetch_radius(
        t.SimpleNamespace(**{**vars(c), "tv_map_radius": 20})) == 53   # 46 + the 6+ mi between them


def test_map_home_setting(app):
    app.save({"tv_map_home": "26.30123,-98.16012"})
    assert app.settings()["tv_map_home"] == "26.30123,-98.16012"
    app.save({"tv_map_home": ""})                            # back to the weather location
    assert app.settings()["tv_map_home"] == ""
    with pytest.raises(ValueError):
        app.save({"tv_map_home": "123 Main St"})             # addresses are looked up first, on the phone


def test_geocode_address_parses_nominatim():
    from feeds import geocode_address

    class S:
        def get(self, url, params=None, headers=None, timeout=None):
            assert "nominatim" in url and "User-Agent" in headers and params["q"] == "1201 W University Dr Edinburg"
            return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: [
                {"display_name": "1201, West University Drive, Edinburg, Texas", "lat": "26.3058123", "lon": "-98.1729876"},
                {"display_name": "broken"}]})()
    assert geocode_address(" 1201 W University Dr Edinburg ", S()) == [
        {"label": "1201, West University Drive, Edinburg, Texas", "lat": 26.30581, "lon": -98.17299}]
