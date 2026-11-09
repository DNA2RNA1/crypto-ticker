"""Flights: parsing the position, route, airline and place feeds (with odd and failing
responses), the fallback source, route caching, tracked-flight states (waiting, in the air,
no signal, landed, next trip), never showing old positions, the screens, and the phone page."""

import json
import time

import pytest
import requests

import flights as fl
import render
from conftest import FakeResponse
from render import Renderer
from sample import sample_flights, sample_tracked
from test_ticker import FakeDisplay
from ticker import Config, Ticker

HOME = (40.0, -90.0)
DEN, BOS = (39.86, -104.67), (42.36, -71.01)


@pytest.fixture(autouse=True)
def no_spacing(monkeypatch):
    monkeypatch.setattr(fl, "MIN_GAP", 0)


def ac(dlat=0.01, dlon=0.0, **kw):
    a = {"hex": "a1b2c3", "flight": "UAL1520 ", "r": "N37502", "t": "B38M", "alt_baro": 8425,
         "gs": 268.4, "track": 285.1, "baro_rate": 2112, "lat": HOME[0] + dlat, "lon": HOME[1] + dlon}
    a.update(kw)
    return a


def route_json(cs="UAL1520", orig=("ORD", 41.98, -87.9), dest=("DEN", *DEN), airline=("United Airlines", "UAL", "UA")):
    return {"response": {"flightroute": {
        "callsign": cs, "airline": {"name": airline[0], "icao": airline[1], "iata": airline[2]},
        "origin": {"iata_code": orig[0], "municipality": "A", "latitude": orig[1], "longitude": orig[2]},
        "destination": {"iata_code": dest[0], "municipality": "B", "latitude": dest[1], "longitude": dest[2]}}}}


class Net:
    """Session stand-in: positions from a queue (or a function of the URL), adsbdb answers
    by callsign/airline, Nominatim from `place`."""

    def __init__(self, positions=None, routes=None, airlines=None, place=None):
        self.positions = list(positions or [])
        self.routes = routes or {}
        self.airlines = airlines or {}
        self.place = place
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        if "adsbdb" in url and "/callsign/" in url:
            item = self.routes.get(url.rsplit("/", 1)[1], FakeResponse(404, {"response": "unknown callsign"}))
        elif "adsbdb" in url and "/airline/" in url:
            item = self.airlines.get(url.rsplit("/", 1)[1], FakeResponse(404, {"response": "unknown airline"}))
        elif "nominatim" in url:
            item = self.place or FakeResponse(200, {"error": "Unable to geocode"})
        else:
            item = self.positions[0](url) if callable(self.positions[0]) else \
                self.positions.pop(0) if len(self.positions) > 1 else self.positions[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def count(self, word):
        return sum(word in u for u in self.calls)


def overhead_feed(net, radius=10):
    f = fl.Flights(*HOME, radius_mi=radius)
    f.lookups.session = net
    return f


# --- parsing ------------------------------------------------------------------------
def test_distance_and_compass():
    assert fl.distance_mi(40, -90, 41, -90) == pytest.approx(69.1, abs=0.2)
    assert [fl.compass(t) for t in (0, 44, 90, 200, 359, None)] == ["N", "NE", "E", "S", "N", ""]


def test_short_airline():
    assert fl.short_airline("Southwest Airlines") == "Southwest"
    assert fl.short_airline("Delta Air Lines") == "Delta"
    assert fl.short_airline("Air Canada") == "Air Canada"
    assert fl.short_airline("") == ""


def test_parse_keeps_planes_in_the_air_within_radius_closest_first():
    data = {"ac": [ac(0.05, flight="FAR1   "), ac(0.01, flight="NEAR1  "),
                   ac(0.0, flight="PARKED ", alt_baro="ground"), ac(0.0, flight="LOW    ", alt_baro=50),
                   ac(1.0, flight="OUTSIDE"), ac(0.0, flight="NOPOS  ", lat=None), "junk", None]}
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
    r = fl.parse_route(route_json())
    assert r["orig"] == "ORD" and r["dest"] == "DEN" and r["dest_pos"] == DEN
    assert r["airline"] == "United Airlines" and r["airline_icao"] == "UAL" and r["airline_iata"] == "UA"


@pytest.mark.parametrize("data", [
    None, {"response": "unknown callsign"}, {"response": {"flightroute": None}},
    {"response": {"flightroute": {"origin": {"iata_code": "ORD"}, "destination": {}}}},
    {"response": {"flightroute": {"origin": {"iata_code": "OR<D"}, "destination": {"iata_code": "DEN"}}}},
])
def test_parse_route_unusable(data):
    assert fl.parse_route(data) is None


@pytest.mark.parametrize("data,expect", [
    ({"address": {"city": "Des Moines", "state": "Iowa", "ISO3166-2-lvl4": "US-IA", "country_code": "us"}},
     "Des Moines, IA"),
    ({"address": {"county": "Kenedy County", "state": "Texas", "ISO3166-2-lvl4": "US-TX", "country_code": "us"}},
     "Kenedy Co, TX"),
    ({"address": {"town": "Reynosa", "state": "Tamaulipas", "country": "Mexico", "country_code": "mx"}},
     "Reynosa, Tamaulipas"),
    ({"error": "Unable to geocode"}, ""), (None, ""), ({"address": "x"}, ""),
])
def test_parse_place(data, expect):
    assert fl.parse_place(data) == expect


@pytest.mark.parametrize("text,expect", [
    ("aa 1234", "AA1234"), ("UA512", "UA512"), ("B6-101", "B6101"), ("AAL1234", "AAL1234"),
    ("ENY3456", "ENY3456"), ("dl 7a", "DL7A"),
])
def test_flight_numbers(text, expect):
    assert fl.parse_flight_number(text) == expect


@pytest.mark.parametrize("text", ["", "1234", "A1", "AA", "12345", "AA12345", "AA1234;rm", "N123AB", "<b>"])
def test_bad_flight_numbers(text):
    with pytest.raises(ValueError):
        fl.parse_flight_number(text)


def test_parse_tracked_list():
    assert fl.parse_tracked("aa 1234\nUA512, aa1234") == ["AA1234", "UA512"]
    with pytest.raises(ValueError):
        fl.parse_tracked("AA1,AA2,AA3,AA4,AA5")
    assert fl.tracked_from_env("AA1,bad!,UA2") == ["AA1", "UA2"]


# --- overhead feed ------------------------------------------------------------------
def test_fetch_adds_routes_and_caches_them():
    net = Net([FakeResponse(200, {"ac": [ac(), ac(0.02, flight="N4521K ", r="N4521K", t="C172")]})],
              {"UAL1520": FakeResponse(200, route_json())})
    f = overhead_feed(net)
    f.data = f.fetch()
    shown = f.overhead(5)
    assert shown[0]["route"]["dest"] == "DEN" and shown[1]["route"] is None
    assert net.count("adsbdb") == 1            # small plane: no route lookup
    f.data = f.fetch()
    assert net.count("adsbdb") == 1            # cached
    assert f.lookups.seen_airlines() == {"UAL": ("UA", "United Airlines")}


def test_unknown_callsign_is_not_asked_again_right_away():
    net = Net([FakeResponse(200, {"ac": [ac(flight="AAL9")]})])
    f = overhead_feed(net)
    f.data = f.fetch()
    f.data = f.fetch()
    assert net.count("adsbdb") == 1 and f.overhead(1)[0]["route"] is None


def test_route_failure_is_retried_next_time():
    net = Net([FakeResponse(200, {"ac": [ac()]})], {"UAL1520": requests.exceptions.ReadTimeout("timed out")})
    f = overhead_feed(net)
    f.data = f.fetch()
    net.routes["UAL1520"] = FakeResponse(200, route_json())
    f.data = f.fetch()
    assert f.overhead(1)[0]["route"]["orig"] == "ORD"


def test_route_lookups_are_capped_per_fetch():
    net = Net([FakeResponse(200, {"ac": [ac(0.001 * i, flight=f"SWA{100 + i}") for i in range(10)]})])
    overhead_feed(net).fetch()
    assert net.count("adsbdb") == fl.ROUTE_LOOKUPS_PER_FETCH


def test_falls_back_to_second_source():
    net = Net([requests.exceptions.ConnectionError("down"), FakeResponse(200, {"ac": [ac()]})])
    f = overhead_feed(net)
    assert len(f.fetch()) == 1 and f.source == "adsb.fi"
    assert "airplanes.live" in net.calls[0] and "adsb.fi" in net.calls[1]


def test_both_sources_down_raises_and_old_positions_expire():
    net = Net([FakeResponse(200, {"ac": [ac()]})])
    f = overhead_feed(net)
    f.data = f.fetch()
    net.positions = [FakeResponse(503, {})]
    with pytest.raises(requests.HTTPError):
        f.fetch()
    assert len(f.overhead(3, now=f.fetched + 60)) == 1
    assert f.overhead(3, now=f.fetched + fl.MAX_AGE + 1) == []


def test_radius_in_request_is_nautical_miles_rounded_up():
    net = Net([FakeResponse(200, {"ac": []})])
    overhead_feed(net, radius=10).fetch()
    assert net.calls[0].endswith("/40.0000/-90.0000/9")


def test_no_location_means_no_calls():
    f = fl.Flights(None, None)
    f.lookups.session = net = Net([FakeResponse(200, {"ac": [ac()]})])
    assert f.fetch() is None and net.calls == []


def test_shared_lookups_send_one_request_at_a_time():
    import threading
    active, peak = [0], [0]

    class Slow:
        def get(self, url, **kw):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            time.sleep(0.02)
            active[0] -= 1
            return FakeResponse(200, {"ac": []})
    lk = fl._Lookups(Slow())
    threads = [threading.Thread(target=lk.get, args=(f"https://h{i}.example/x",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1


def test_requests_to_one_host_are_spaced(monkeypatch):
    monkeypatch.setattr(fl, "MIN_GAP", 0.05)
    fl._hosts.clear()
    start = time.monotonic()
    for _ in range(3):
        fl._throttle("https://api.airplanes.live/v2/x")
    assert time.monotonic() - start >= 0.1


# --- tracked flights ----------------------------------------------------------------
class Sky:
    """Where DAL1189 is: set .plane to a readsb row, or None when no receiver hears it."""

    def __init__(self):
        self.plane = None

    def __call__(self, url):
        return FakeResponse(200, {"ac": [self.plane] if self.plane else []})


def tracker(tmp_path, flights=("DL1189",), **net_kw):
    sky = Sky()
    net = Net([sky], {"DAL1189": FakeResponse(200, route_json("DAL1189", ("DEN", *DEN), ("BOS", *BOS),
                                                                ("Delta Air Lines", "DAL", "DL")))}, **net_kw)
    t = fl.Tracker(list(flights), state_path=str(tmp_path / "tracked.json"))
    t.lookups.session = net
    t.session = net
    return t, sky, net


def plane(lat, lon, alt=36000, **kw):
    return dict({"flight": "DAL1189 ", "t": "A321", "alt_baro": alt, "gs": 455, "track": 80, "baro_rate": 0,
                 "lat": lat, "lon": lon}, **kw)


def test_flight_number_becomes_callsign_and_waits(tmp_path):
    t, sky, net = tracker(tmp_path)
    assert t.rows()[0]["status"] == "checking"
    t.data = t.fetch()
    row = t.rows()[0]
    assert row["callsign"] == "DAL1189" and row["status"] == "waiting" and row["route"]["dest"] == "BOS"
    assert net.count("/airline/") == 0          # DL is in the built-in table


def test_leading_zeros_are_dropped_from_the_callsign(tmp_path):
    t, sky, net = tracker(tmp_path)
    assert t.lookups.callsign_for("AA0012") == "AAL12"
    assert t.lookups.callsign_for("AAL0012") == "AAL12"
    assert t.lookups.callsign_for("DL1189") == "DAL1189"


def test_unknown_airline_prefix_is_looked_up(tmp_path):
    t, sky, net = tracker(tmp_path, ("XQ123",),
                          airlines={"XQ": FakeResponse(200, {"response": [{"icao": "SXS", "iata": "XQ", "name": "SunExpress"}]})})
    t.data = t.fetch()
    assert t.rows()[0]["callsign"] == "SXS123"
    t2, _, _ = tracker(tmp_path, ("ZZ9",))
    t2.data = t2.fetch()
    assert t2.rows()[0]["error"] == "airline not found" and t2.rows()[0]["status"] == "waiting"


def test_in_the_air_with_place_progress_and_eta(tmp_path):
    place = FakeResponse(200, {"address": {"city": "Des Moines", "state": "Iowa", "ISO3166-2-lvl4": "US-IA",
                                           "country_code": "us"}})
    t, sky, net = tracker(tmp_path, place=place)
    sky.plane = plane(41.6, -93.6)
    t.data = t.fetch()
    r = t.rows()[0]
    assert r["status"] == "air" and r["place"] == "Des Moines, IA" and r["alt_ft"] == 36000
    assert 0.3 < r["progress"] < 0.45
    hours = (r["eta"] - r["seen_at"]) / 3600
    assert hours == pytest.approx(r["remaining_mi"] / (455 * fl.MI_PER_NM), rel=0.01)
    t.data = t.fetch()
    assert net.count("nominatim") == 1          # place looked up at most every 5 minutes


def test_landing_by_ground_report(tmp_path):
    t, sky, net = tracker(tmp_path)
    sky.plane = plane(41.0, -80.0)
    t.data = t.fetch()
    sky.plane = plane(*BOS, alt="ground", gs=10)
    t.data = t.fetch()
    r = t.rows()[0]
    assert r["status"] == "landed" and r["progress"] == 1.0 and r["landed_at"]


def test_landing_by_dropping_out_low_near_destination(tmp_path):
    t, sky, net = tracker(tmp_path)
    sky.plane = plane(BOS[0] + 0.1, BOS[1] - 0.1, alt=2000, baro_rate=-800)
    t.data = t.fetch()
    sky.plane = None
    st = t.state["DL1189"]
    st["seen_at"] -= 5 * 60                     # gone for 5 minutes
    t.data = t.fetch()
    assert t.rows()[0]["status"] == "landed"


def test_dropping_out_high_mid_flight_is_no_signal_not_landed(tmp_path):
    t, sky, net = tracker(tmp_path)
    sky.plane = plane(41.0, -85.0)
    t.data = t.fetch()
    sky.plane = None
    t.state["DL1189"]["seen_at"] -= 5 * 60
    t.data = t.fetch()
    assert t.state["DL1189"]["status"] == "air"
    assert t.rows()[0]["status"] == "lost"       # not refreshed lately: not shown as live
    t.state["DL1189"]["seen_at"] -= fl.LOST_AFTER
    t.data = t.fetch()
    assert t.rows()[0]["status"] == "lost"
    sky.plane = plane(42.0, -75.0)               # back in range
    t.data = t.fetch()
    assert t.rows()[0]["status"] == "air"


def test_on_ground_before_departure(tmp_path):
    t, sky, net = tracker(tmp_path)
    sky.plane = plane(*DEN, alt="ground", gs=0)
    t.data = t.fetch()
    assert t.rows()[0]["status"] == "ground"


def test_landed_shows_for_hours_then_waits_for_next_trip(tmp_path):
    t, sky, net = tracker(tmp_path)
    sky.plane = plane(41.0, -80.0)
    t.data = t.fetch()
    sky.plane = plane(*BOS, alt="ground")
    t.data = t.fetch()
    sky.plane = None
    t.data = t.fetch()
    assert t.rows()[0]["status"] == "landed"
    t.state["DL1189"]["landed_at"] -= fl.LANDED_SHOW + 1
    t.data = t.fetch()
    assert t.rows()[0]["status"] == "waiting"


def test_state_survives_restart(tmp_path):
    t, sky, net = tracker(tmp_path)
    sky.plane = plane(41.0, -80.0)
    t.data = t.fetch()
    sky.plane = plane(*BOS, alt="ground")
    t.data = t.fetch()
    saved = json.loads((tmp_path / "tracked.json").read_text())
    assert saved["DL1189"]["status"] == "landed"
    t2 = fl.Tracker(["DL1189"], state_path=str(tmp_path / "tracked.json"))
    assert t2.state["DL1189"]["status"] == "landed"
    t3 = fl.Tracker(["UA1"], state_path=str(tmp_path / "tracked.json"))
    assert t3.state == {}                         # removed flights are dropped


def test_one_flight_failing_keeps_the_others(tmp_path):
    t, sky, net = tracker(tmp_path, ("DL1189", "UA77"))
    sky.plane = plane(41.0, -80.0)
    original = net.get

    def get(url, **kw):
        if "UAL77" in url and "adsbdb" not in url:
            raise requests.exceptions.ConnectionError("down")
        return original(url, **kw)
    net.get = get
    t.data = t.fetch()
    rows = {r["flight"]: r for r in t.rows()}
    assert rows["DL1189"]["status"] == "air" and "unavailable" in rows["UA77"]["error"]


def test_status_for_phone_page(tmp_path):
    t, sky, net = tracker(tmp_path)
    t.data = t.fetch()
    s = t.status_for()["DL1189"]
    assert s["route"] == "DEN-BOS" and s["callsign"] == "DAL1189" and s["status"] == "waiting"


# --- the screens ---------------------------------------------------------------------
def test_overhead_screens_draw_without_errors():
    r = Renderer(64, 32)
    for p in sample_flights():
        for metric in (False, True):
            img = r.flight(p, metric).first_frame()
            assert img.size == (64, 32) and img.getbbox()


def test_tracked_screens_draw_every_state():
    from zoneinfo import ZoneInfo
    r = Renderer(64, 32)
    rows = sample_tracked(time.time())
    rows += [dict(rows[0], status=s) for s in ("lost", "ground", "checking")]
    rows += [dict(rows[2], error="airline not found", route=None, callsign="")]
    for row in rows:
        img = r.tracked_flight(row, ZoneInfo("America/Chicago")).first_frame()
        assert img.size == (64, 32) and img.getbbox()


def test_accented_names_draw_without_question_marks():
    r = Renderer(64, 32)
    f = r.fonts["5x8"]
    assert "?" not in Renderer._panel_text(f, "Aeroméxico") and Renderer._panel_text(f, "Aeroméxico").startswith("Aerom")
    p = dict(sample_flights()[0], route=dict(sample_flights()[0]["route"], airline="Aeroméxico Connect"))
    assert r.flight(p).first_frame().getbbox()
    row = dict(sample_tracked(time.time())[0], place="San Nicolás de los Garza, NLE")
    assert r.tracked_flight(row).first_frame().getbbox()


def test_broken_picture_file_falls_back_to_the_tile(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "AIRLINE_DIR", str(tmp_path))
    (tmp_path / "UAL.png").write_bytes(b"not a picture")
    tile = Renderer(64, 32).airline_badge("UAL", "UA")
    assert tile.getpixel((8, 1)) == Renderer.AIRLINE_COLORS["UAL"][0]


def test_landed_is_green_and_bar_full():
    r = Renderer(64, 32)
    landed = r.tracked_flight(sample_tracked(time.time())[1]).first_frame()
    assert sum(landed.getpixel((x, 28)) == Renderer.CLIMB for x in range(1, 20)) == 19


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


def test_badges(tmp_path, monkeypatch):
    from PIL import Image
    monkeypatch.setattr(render, "AIRLINE_DIR", str(tmp_path))
    r = Renderer(64, 32)
    united = r.airline_badge("UAL", "UA")
    assert united.getpixel((8, 1)) == Renderer.AIRLINE_COLORS["UAL"][0]
    other = r.airline_badge("XYZ")
    assert other.size == (16, 16) and other.getbbox()
    small = r.airline_badge("")
    assert small.getpixel((7, 7)) != (0, 0, 0)          # the plane picture
    Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(tmp_path / "UAL.png")
    mine = r.airline_badge("UAL", "UA")
    assert mine.getpixel((8, 8))[0] > 200 and mine.getpixel((8, 8))[2] < 50


# --- in the ticker ---------------------------------------------------------------------
def test_flights_screen_in_ticker(tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "STATE_PATH", str(tmp_path / "t.json"))
    cfg = Config()
    cfg.screens = ["flights"]
    cfg.flights_per_cycle = 2
    cfg.flights_track = ["DL1189", "AA2410"]
    t = Ticker(cfg, FakeDisplay())
    t.flights.update = lambda now=None: None
    t.tracker.update = lambda now=None: None
    t.tracker.rows = lambda now=None: sample_tracked(time.time())[:2]
    assert len(t.screens()) == 2                 # nothing overhead: only the tracked flights
    t.flights.overhead = lambda n, now=None: sample_flights()[:n]
    screens = t.screens()
    assert len(screens) == 4 and all(s().first_frame().size == (64, 32) for s in screens)
    t.cfg.flights_overhead = False
    assert len(t.screens()) == 2


def test_empty_sky_with_only_flights_on_shows_the_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "STATE_PATH", str(tmp_path / "t.json"))
    cfg = Config()
    cfg.screens = ["flights"]
    d = FakeDisplay()
    t = Ticker(cfg, d)
    t.flights.update = lambda now=None: None
    t.weather.update = lambda now=None: None
    t.step()
    assert len(d.frames) >= 1                 # the time, not the last plane left up
    t.flights.overhead = lambda n, now=None: sample_flights()[:1]
    before = len(d.frames)
    t.step()
    assert len(d.frames) > before


def test_flights_off_makes_no_calls():
    cfg = Config()
    cfg.screens = ["clock", "coins"]
    cfg.flights_track = ["DL1189"]
    t = Ticker(cfg, FakeDisplay())
    called = []
    t.flights.update = lambda now=None: called.append(1)
    t.tracker.update = lambda now=None: called.append(2)
    t.screens()
    assert called == []


def test_settings(monkeypatch):
    c = Config.from_env()
    assert "flights" not in c.screens and c.flights_radius == 10 and c.flights_per_cycle == 3
    assert c.flights_overhead and c.flights_track == []
    monkeypatch.setenv("SCREENS", "flights,coins")
    monkeypatch.setenv("FLIGHTS_RADIUS", "25")
    monkeypatch.setenv("FLIGHTS_PER_CYCLE", "9")
    monkeypatch.setenv("FLIGHTS_OVERHEAD", "off")
    monkeypatch.setenv("FLIGHTS_TRACK", "AA1234,UA512")
    c = Config.from_env()
    assert c.screens == ["flights", "coins"] and c.flights_radius == 25 and c.flights_per_cycle == 5
    assert not c.flights_overhead and c.flights_track == ["AA1234", "UA512"]
    monkeypatch.setenv("FLIGHTS_RADIUS", "7")
    assert Config.from_env().flights_radius == 10


def test_reload_rebuilds_feeds_when_settings_change(tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "STATE_PATH", str(tmp_path / "t.json"))
    env = tmp_path / "settings.env"
    env.write_text("SCREENS=clock\nFLIGHTS_RADIUS=10\n")
    from ticker import load_env_file
    load_env_file(str(env), override=True)
    t = Ticker(Config.from_env(), FakeDisplay(), env_path=str(env))
    before, tracker_before = t.flights, t.tracker
    env.write_text("SCREENS=clock\nFLIGHTS_RADIUS=25\nFLIGHTS_TRACK=DL1189\n")
    t.reload()
    assert t.flights is not before and t.flights.radius_mi == 25
    assert t.tracker is not tracker_before and t.tracker.flights == ["DL1189"]
    assert t.tracker.lookups is t.flights.lookups


# --- phone page ---------------------------------------------------------------------
def test_settings_page_saves_flight_options(app):
    saved = app.save({"screens": "flights,coins", "flights_radius": "15", "flights_per_cycle": 2,
                      "flights_overhead": "off", "flights_track": "aa 1234\nUA512"})
    assert saved["FLIGHTS_RADIUS"] == "15" and saved["FLIGHTS_PER_CYCLE"] == "2"
    assert saved["FLIGHTS_OVERHEAD"] == "off" and saved["FLIGHTS_TRACK"] == "AA1234,UA512"
    s = app.settings()
    assert s["flights_radius"] == 15 and s["flights_overhead"] == "off"
    assert s["flights_track"] == ["AA1234", "UA512"] and "flights" in s["screen_names"]
    assert {a["icao"] for a in s["airlines"]} >= {"AAL", "UAL"}


@pytest.mark.parametrize("field,value", [
    ("flights_radius", "7"), ("flights_radius", "10\nWEB_PIN="), ("flights_per_cycle", 0),
    ("flights_per_cycle", 6), ("flights_overhead", "maybe"), ("flights_track", "AA1\nWEB_PIN=1"),
    ("flights_track", "AA1,AA2,AA3,AA4,AA5"), ("flights_track", "N123AB"),
])
def test_settings_page_rejects_bad_flight_options(app, field, value):
    before = open(app.env_path).read()
    with pytest.raises(ValueError):
        app.save({field: value})
    assert open(app.env_path).read() == before


def test_airline_picture_upload_and_reset(app, tmp_path, monkeypatch):
    import io

    import webui
    from PIL import Image
    monkeypatch.setattr(webui, "AIRLINE_DIR", str(tmp_path / "air"))
    monkeypatch.setattr(render, "AIRLINE_DIR", str(tmp_path / "air"))
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (255, 0, 0)).save(buf, "PNG")
    app.save_airline_icon("ual", buf.getvalue())
    assert (tmp_path / "air" / "UAL.png").exists()
    assert any(a["icao"] == "UAL" and a["custom"] for a in app.settings()["airlines"])
    png = app.airline_badge_png("UAL", "UA")
    assert Image.open(io.BytesIO(png)).size == (64, 64)
    app.reset_airline_icon("UAL")
    assert not (tmp_path / "air" / "UAL.png").exists()
    for bad in ("../x", "Z9", "UALX", "u1l"):  # Z9: a 2-letter code it doesn't know
        with pytest.raises(ValueError):
            app.save_airline_icon(bad, buf.getvalue())
    app.save_airline_icon("ua", buf.getvalue())  # the 2-letter code works too
    assert (tmp_path / "air" / "UAL.png").exists()
    app.reset_airline_icon("UAL")
    assert any(a["icao"] == "AAL" for a in app.settings()["airlines"])  # big airlines listed from the start
    with pytest.raises(ValueError):
        app.save_airline_icon("DAL", b"not a picture")
    with pytest.raises(ValueError):
        app.airline_badge_png("UAL", "U<")


def test_airline_routes_need_login_and_check_codes(server):
    from conftest import login, request
    assert request(server, "POST", "/api/airline-icon?code=UAL", b"x")[0] == 401
    assert request(server, "POST", "/api/airline-icon/reset?code=UAL")[0] == 401
    auth = login(server)
    assert request(server, "GET", "/api/airline-badge?code=../x", headers=auth)[0] == 400
    assert request(server, "POST", "/api/airline-icon?code=UAL", b"not a picture", auth)[0] == 400


# --- out-of-date routes ---------------------------------------------------------------
DFW, MFE, DCA, BOS = (32.897, -97.038), (26.176, -98.239), (38.852, -77.037), (42.364, -71.005)


def _route(a, b, names):
    return {"orig": names[0], "dest": names[1], "orig_pos": a, "dest_pos": b}


def test_route_must_fit_where_the_plane_is():
    from flights import route_fits
    near_mfe = (26.35, -98.10)  # a plane over Edinburg
    assert route_fits(_route(DFW, MFE, "DM"), *near_mfe)
    assert not route_fits(_route(DCA, BOS, "DB"), *near_mfe)   # the AAL2029 mix-up
    assert route_fits(_route(DCA, BOS, "DB"), 40.6, -74.0)     # over New York: fine
    assert route_fits(_route(DFW, MFE, "DM"), 29.5, -97.2)     # mid-route, a little off the line
    assert route_fits(None, *near_mfe) and route_fits({"orig": "X"}, *near_mfe)  # nothing to judge by
    assert route_fits(_route(DCA, BOS, "DB"), None, None)


def test_overhead_drops_a_route_the_plane_cant_be_on():
    import flights as fl
    f = fl.Flights.__new__(fl.Flights)
    f.data = [{"callsign": "AAL2029", "lat": 26.35, "lon": -98.10, "dist_mi": 5.0},
              {"callsign": "AAL1450", "lat": 26.4, "lon": -98.0, "dist_mi": 9.0}]
    f.fetched = 1000
    routes = {"AAL2029": _route(DCA, BOS, "DB"), "AAL1450": _route(DFW, MFE, "DM")}
    f.route = lambda cs, now=None: routes[cs]
    out = f.overhead(2, now=1000)
    assert out[0]["route"] is None and out[1]["route"]["orig"] == "D"
