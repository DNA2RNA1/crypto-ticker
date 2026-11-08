"""Planes overhead: parsing the position and route feeds (with odd and failing responses),
the fallback source, route caching, never showing old positions, and the screen itself."""

import pytest
import requests

import flights as fl
from conftest import FakeResponse
from render import Renderer
from sample import sample_flights
from test_ticker import FakeDisplay
from ticker import Config, Ticker

HOME = (40.0, -90.0)


def ac(dlat=0.01, dlon=0.0, **kw):
    a = {"hex": "a1b2c3", "flight": "UAL1520 ", "r": "N37502", "t": "B38M", "alt_baro": 8425,
         "gs": 268.4, "track": 285.1, "baro_rate": 2112, "lat": HOME[0] + dlat, "lon": HOME[1] + dlon}
    a.update(kw)
    return a


ROUTE = {"response": {"flightroute": {
    "callsign": "UAL1520", "airline": {"name": "United Airlines", "icao": "UAL"},
    "origin": {"iata_code": "ORD", "icao_code": "KORD", "municipality": "Chicago"},
    "destination": {"iata_code": "DEN", "icao_code": "KDEN", "municipality": "Denver"}}}}


class Routes:
    """Session stand-in: positions from one queue, adsbdb answers by callsign."""

    def __init__(self, positions, routes=None):
        self.positions = list(positions)
        self.routes = routes or {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if "adsbdb" in url:
            item = self.routes.get(url.rsplit("/", 1)[1], FakeResponse(404, {"response": "unknown callsign"}))
        else:
            item = self.positions.pop(0) if len(self.positions) > 1 else self.positions[0]
        if isinstance(item, BaseException):
            raise item
        return item


# --- parsing ------------------------------------------------------------------------
def test_distance_is_in_miles():
    assert fl.distance_mi(40, -90, 41, -90) == pytest.approx(69.1, abs=0.2)


def test_parse_keeps_planes_in_the_air_within_radius_closest_first():
    data = {"ac": [
        ac(0.05, flight="FAR1   "),
        ac(0.01, flight="NEAR1  "),
        ac(0.0, flight="PARKED ", alt_baro="ground"),
        ac(0.0, flight="LOW    ", alt_baro=50),
        ac(1.0, flight="OUTSIDE"),          # ~69 miles away
        ac(0.0, flight="NOPOS  ", lat=None),
        "junk", None,
    ]}
    planes = fl.parse_aircraft(data, *HOME, radius_mi=10)
    assert [p["callsign"] for p in planes] == ["NEAR1", "FAR1"]
    p = planes[0]
    assert p["alt_ft"] == 8425 and p["type"] == "B38M" and p["reg"] == "N37502" and p["vrate_fpm"] == 2112
    assert p["dist_mi"] == pytest.approx(0.69, abs=0.02)


def test_parse_older_format_and_odd_values():
    data = {"aircraft": [ac(flight="<script>", r="N1;rm", alt_baro=None, alt_geom=5000, gs="fast",
                            baro_rate=None, geom_rate=-640, track=float("nan"))]}
    p = fl.parse_aircraft(data, *HOME, radius_mi=5)[0]
    assert p["callsign"] == "" and p["reg"] == "" and p["alt_ft"] == 5000
    assert p["speed_kt"] is None and p["vrate_fpm"] == -640 and p["track"] is None


@pytest.mark.parametrize("data", [None, [], "x", {"ac": "x"}, {"msg": "No error"}])
def test_parse_rejects_bad_payloads(data):
    with pytest.raises(ValueError):
        fl.parse_aircraft(data, *HOME, 10)


def test_parse_route():
    r = fl.parse_route(ROUTE)
    assert r == {"orig": "ORD", "dest": "DEN", "orig_city": "Chicago", "dest_city": "Denver",
                 "airline": "United Airlines"}


@pytest.mark.parametrize("data", [
    None, {"response": "unknown callsign"}, {"response": {"flightroute": None}},
    {"response": {"flightroute": {"origin": {"iata_code": "ORD"}, "destination": {}}}},
    {"response": {"flightroute": {"origin": {"iata_code": "OR<D"}, "destination": {"iata_code": "DEN"}}}},
])
def test_parse_route_unusable(data):
    assert fl.parse_route(data) is None


# --- the feed -----------------------------------------------------------------------
def test_fetch_adds_routes_and_caches_them():
    f = fl.Flights(*HOME, radius_mi=10)
    f.session = Routes([FakeResponse(200, {"ac": [ac(), ac(0.02, flight="N4521K ", r="N4521K", t="C172")]})],
                       {"UAL1520": FakeResponse(200, ROUTE)})
    f.data = f.fetch()
    shown = f.overhead(5)
    assert shown[0]["route"]["dest"] == "DEN" and shown[1]["route"] is None
    assert sum("adsbdb" in u for u in f.session.calls) == 1   # small plane: no route lookup
    f.data = f.fetch()
    assert sum("adsbdb" in u for u in f.session.calls) == 1   # cached


def test_unknown_callsign_is_not_asked_again_right_away():
    f = fl.Flights(*HOME)
    f.session = Routes([FakeResponse(200, {"ac": [ac(flight="AAL9")]})])
    f.data = f.fetch()
    f.data = f.fetch()
    assert sum("adsbdb" in u for u in f.session.calls) == 1 and f.overhead(1)[0]["route"] is None


def test_route_failure_is_retried_next_time():
    f = fl.Flights(*HOME)
    f.session = Routes([FakeResponse(200, {"ac": [ac()]})],
                       {"UAL1520": requests.exceptions.ReadTimeout("timed out")})
    f.data = f.fetch()
    f.session.routes["UAL1520"] = FakeResponse(200, ROUTE)
    f.data = f.fetch()
    assert f.overhead(1)[0]["route"]["orig"] == "ORD"


def test_route_lookups_are_capped_per_fetch():
    planes = [ac(0.001 * i, flight=f"SWA{100 + i}") for i in range(10)]
    f = fl.Flights(*HOME)
    f.session = Routes([FakeResponse(200, {"ac": planes})])
    f.fetch()
    assert sum("adsbdb" in u for u in f.session.calls) == fl.ROUTE_LOOKUPS_PER_FETCH


def test_falls_back_to_second_source():
    f = fl.Flights(*HOME)
    f.session = Routes([requests.exceptions.ConnectionError("down"), FakeResponse(200, {"ac": [ac()]})])
    assert len(f.fetch()) == 1 and f.source == "adsb.fi"
    assert "airplanes.live" in f.session.calls[0] and "adsb.fi" in f.session.calls[1]


def test_both_sources_down_raises_and_keeps_nothing_old():
    f = fl.Flights(*HOME)
    f.session = Routes([FakeResponse(200, {"ac": [ac()]})])
    f.data = f.fetch()
    f.session = Routes([FakeResponse(503, {})])
    with pytest.raises(requests.HTTPError):
        f.fetch()
    assert len(f.overhead(3, now=f.fetched + 60)) == 1         # still fresh enough
    assert f.overhead(3, now=f.fetched + fl.MAX_AGE + 1) == []  # too old: skip the screen


def test_radius_in_request_is_nautical_miles_rounded_up():
    f = fl.Flights(*HOME, radius_mi=10)
    f.session = Routes([FakeResponse(200, {"ac": []})])
    f.fetch()
    assert f.session.calls[0].endswith("/40.0000/-90.0000/9")


def test_no_location_means_no_calls():
    f = fl.Flights(None, None)
    f.session = Routes([FakeResponse(200, {"ac": [ac()]})])
    assert f.fetch() is None and f.session.calls == []


# --- the screen ---------------------------------------------------------------------
def test_screen_draws_every_kind_of_plane_without_errors():
    r = Renderer(64, 32)
    for p in sample_flights():
        for metric in (False, True):
            img = r.flight(p, metric).first_frame()
            assert img.size == (64, 32) and img.getbbox()


def test_numbers():
    p = sample_flights()[0]
    assert Renderer.flight_numbers(p) == ("2.4MI", "8,425FT", "308MPH")
    assert Renderer.flight_numbers(p, metric=True) == ("3.9KM", "2,568M", "496KMH")
    far = dict(p, dist_mi=12.3, speed_kt=None)
    assert Renderer.flight_numbers(far)[0] == "12MI" and Renderer.flight_numbers(far)[2] == ""


def test_climb_and_descent_arrows():
    r = Renderer(64, 32)
    up, down, level = (r.flight(p).first_frame() for p in sample_flights())
    colors = lambda img: {img.getpixel((x, y)) for x in range(64) for y in range(26, 31)}
    assert Renderer.CLIMB in colors(up) and Renderer.DESCEND in colors(down)
    assert not {Renderer.CLIMB, Renderer.DESCEND} & colors(level)


def test_long_airline_name_scrolls_short_one_does_not():
    r = Renderer(64, 32)
    long_name, small = sample_flights()[1], sample_flights()[2]
    assert r.flight(long_name).scrolls and not r.flight(small).scrolls
    s = r.flight(long_name)
    assert s.first_frame().tobytes() != s.last_frame().tobytes()


def test_flights_screen_in_ticker():
    cfg = Config()
    cfg.screens = ["flights"]
    cfg.flights_per_cycle = 2
    t = Ticker(cfg, FakeDisplay())
    t.flights.update = lambda now=None: None
    assert t.screens() == []                     # nothing overhead: skipped
    t.flights.overhead = lambda n, now=None: sample_flights()[:n]
    screens = t.screens()
    assert len(screens) == 2 and screens[0]().first_frame().size == (64, 32)


def test_flights_off_makes_no_calls():
    cfg = Config()
    cfg.screens = ["clock", "coins"]
    t = Ticker(cfg, FakeDisplay())
    called = []
    t.flights.update = lambda now=None: called.append(1)
    t.screens()
    assert called == []


def test_settings(monkeypatch):
    c = Config.from_env()
    assert "flights" not in c.screens and c.flights_radius == 10 and c.flights_per_cycle == 3
    monkeypatch.setenv("SCREENS", "flights,coins")
    monkeypatch.setenv("FLIGHTS_RADIUS", "25")
    monkeypatch.setenv("FLIGHTS_PER_CYCLE", "9")
    c = Config.from_env()
    assert c.screens == ["flights", "coins"] and c.flights_radius == 25 and c.flights_per_cycle == 5
    monkeypatch.setenv("FLIGHTS_RADIUS", "7")
    assert Config.from_env().flights_radius == 10


def test_reload_rebuilds_feed_when_area_changes(monkeypatch, tmp_path):
    env = tmp_path / "settings.env"
    env.write_text("SCREENS=flights\nFLIGHTS_RADIUS=10\n")
    from ticker import load_env_file
    load_env_file(str(env), override=True)
    t = Ticker(Config.from_env(), FakeDisplay(), env_path=str(env))
    before = t.flights
    env.write_text("SCREENS=flights\nFLIGHTS_RADIUS=25\n")
    t.reload()
    assert t.flights is not before and t.flights.radius_mi == 25


def test_settings_page_saves_flight_options(app):
    saved = app.save({"screens": "flights,coins", "flights_radius": "15", "flights_per_cycle": 2})
    assert saved["FLIGHTS_RADIUS"] == "15" and saved["FLIGHTS_PER_CYCLE"] == "2"
    s = app.settings()
    assert s["flights_radius"] == 15 and s["flights_per_cycle"] == 2 and "flights" in s["screen_names"]


@pytest.mark.parametrize("field,value", [("flights_radius", "7"), ("flights_radius", "10\nWEB_PIN="),
                                         ("flights_per_cycle", 0), ("flights_per_cycle", 6)])
def test_settings_page_rejects_bad_flight_options(app, field, value):
    before = open(app.env_path).read()
    with pytest.raises(ValueError):
        app.save({field: value})
    assert open(app.env_path).read() == before
