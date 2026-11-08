"""Flights: planes near you, and flights you're following (the "flights" screen).

Planes overhead: the closest aircraft in the air around the weather location, with route,
airline, type, altitude and speed.

Tracked flights: flight numbers you enter on the phone page (AA1234, UA 512, or an ICAO
callsign like ENY3456). Each shows its route, where it is now ("over Austin, TX"), how far
along it is with an estimated arrival time, and LANDED once it's down.

    positions   airplanes.live  /v2/point/<lat>/<lon>/<nm>, /v2/callsign/<CALLSIGN>
                adsb.fi         /api/v3/lat/../lon/../dist/<nm>, /api/v2/callsign/<CALLSIGN>
    routes      adsbdb.com      /v0/callsign/<CALLSIGN>, /v0/airline/<IATA>
    places      OpenStreetMap Nominatim reverse geocoding (city and state under a plane)

All are free for personal, non-commercial use and need no key. Requests to each host are
spaced at least a second apart (their published limit). Nothing is fetched while the
flights screen is off. Positions older than two minutes are never shown as current.

ADS-B only covers aircraft in range of a volunteer receiver, so a plane can drop out over
open water; it shows as NO SIGNAL with where it was last seen until it reappears. Arrival
times are estimates from the remaining distance and ground speed, not airline schedules.
"""

import json
import logging
import math
import os
import re
import threading
import time
from urllib.parse import urlparse

from feeds import Feed

log = logging.getLogger("ticker.flights")

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "cache", "flights_tracked.json")

POINT_SOURCES = (
    ("airplanes.live", "https://api.airplanes.live/v2/point/{lat:.4f}/{lon:.4f}/{nm}"),
    ("adsb.fi", "https://opendata.adsb.fi/api/v3/lat/{lat:.4f}/lon/{lon:.4f}/dist/{nm}"),
)
CALLSIGN_SOURCES = (
    ("airplanes.live", "https://api.airplanes.live/v2/callsign/{}"),
    ("adsb.fi", "https://opendata.adsb.fi/api/v2/callsign/{}"),
)
ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{}"
AIRLINE_URL = "https://api.adsbdb.com/v0/airline/{}"
REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
# Nominatim asks for an honest app name rather than a browser's
GEOCODE_UA = "crypto-ticker-led-display/1.0 (personal LED panel; few requests)"

RADIUS_CHOICES = (3, 5, 10, 15, 25)      # miles
MAX_TRACKED = 4
MAX_AGE = 120                            # seconds before positions count as too old to show
ROUTE_TTL = 12 * 3600
ROUTE_MISS_TTL = 6 * 3600
ROUTE_LOOKUPS_PER_FETCH = 4
PLACE_EVERY = 300                        # seconds between "flying over" lookups per flight
LANDED_SHOW = 6 * 3600                   # LANDED stays up this long, then it waits for the next trip
LOST_AFTER = 15 * 60                     # gone this long mid-flight: NO SIGNAL
LOST_FORGET = 24 * 3600
LANDING_ALT_FT = 5000                    # last seen this low near the destination, then gone: landed
LANDING_DIST_MI = 30
MIN_GAP = 1.1                            # seconds between requests to one host

AIRLINE_CALLSIGN = re.compile(r"[A-Z]{3}\d{1,4}[A-Z]{0,2}")  # AAL1234, SWA22 (not N123AB)
CALLSIGN_OK = re.compile(r"[A-Z0-9]{2,8}")
FLIGHT_RE = re.compile(r"([A-Z0-9]{2}|[A-Z]{3})(\d{1,4}[A-Z]?)")
MI_PER_NM = 1.15078
EARTH_MI = 3958.8

# IATA -> ICAO for common airlines, so "AA1234" finds callsign AAL1234 without a lookup.
# Anything else is looked up on adsbdb. Regional partners fly under their own callsign
# (an American Eagle flight may be ENY3456): enter that callsign if the flight number
# never shows up.
IATA_ICAO = {
    "AA": "AAL", "UA": "UAL", "DL": "DAL", "WN": "SWA", "B6": "JBU", "AS": "ASA", "NK": "NKS",
    "F9": "FFT", "G4": "AAY", "HA": "HAL", "SY": "SCX", "MX": "MXY", "XP": "CXP", "QX": "QXE",
    "OO": "SKW", "YX": "RPA", "MQ": "ENY", "9E": "EDV", "OH": "JIA", "YV": "ASH", "ZW": "AWI",
    "AC": "ACA", "WS": "WJA", "AM": "AMX", "Y4": "VOI", "VB": "VIV", "4O": "AIJ", "CM": "CMP",
    "AV": "AVA", "LA": "LAN", "BA": "BAW", "VS": "VIR", "LH": "DLH", "AF": "AFR", "KL": "KLM",
    "IB": "IBE", "EK": "UAE", "QR": "QTR", "TK": "THY", "JL": "JAL", "NH": "ANA", "KE": "KAL",
    "CX": "CPA", "SQ": "SIA", "QF": "QFA", "NZ": "ANZ", "FX": "FDX", "5X": "UPS",
}

_hosts = {}
_hosts_lock = threading.Lock()


def _throttle(url):
    """Keep at least MIN_GAP between requests to the same host, across both feeds."""
    host = urlparse(url).hostname or ""
    with _hosts_lock:
        now = time.monotonic()
        wait = max(0.0, _hosts.get(host, 0.0) + MIN_GAP - now)
        _hosts[host] = now + wait
    if wait:
        time.sleep(wait)


def distance_mi(lat1, lon1, lat2, lon2):
    """Great-circle distance in statute miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_MI * math.asin(min(1.0, math.sqrt(a)))


def compass(track):
    if track is None:
        return ""
    return "N NE E SE S SW W NW".split()[int((track % 360 + 22.5) // 45) % 8]


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def short_airline(name):
    """'Southwest Airlines' -> 'Southwest', 'Delta Air Lines' -> 'Delta'; others unchanged."""
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    short = re.sub(r"\s+(Air Lines|Airlines|Airways|Air Line|Aviation)$", "", name, flags=re.I)
    return short or name


# --- parsing ------------------------------------------------------------------------
def _rows(data):
    if not isinstance(data, dict):
        raise ValueError("unexpected flight data")
    rows = data.get("ac")
    if rows is None:
        rows = data.get("aircraft")
    if not isinstance(rows, list):
        raise ValueError("unexpected flight data")
    return [a for a in rows if isinstance(a, dict)]


def parse_one(a):
    """One readsb aircraft -> our dict, or None without a usable position.
    'ground' is True for aircraft on the ground (alt_ft is then 0)."""
    plat, plon = _num(a.get("lat")), _num(a.get("lon"))
    if plat is None or plon is None or not (-90 <= plat <= 90 and -180 <= plon <= 180):
        return None
    alt = a.get("alt_baro")
    ground = alt == "ground" or bool(a.get("ground"))
    alt = 0.0 if ground else _num(alt if alt is not None else a.get("alt_geom"))
    if alt is None:
        return None
    callsign = str(a.get("flight") or "").strip().upper()
    reg = str(a.get("r") or "").strip().upper()
    return {
        "callsign": callsign if CALLSIGN_OK.fullmatch(callsign) else "",
        "reg": reg[:10] if re.fullmatch(r"[A-Z0-9\-]{2,10}", reg) else "",
        "type": str(a.get("t") or "").strip().upper()[:6],
        "ground": ground,
        "alt_ft": int(round(alt)),
        "speed_kt": _num(a.get("gs")),
        "vrate_fpm": _num(a.get("baro_rate") if a.get("baro_rate") is not None else a.get("geom_rate")),
        "track": _num(a.get("track")),
        "lat": plat, "lon": plon,
    }


def parse_aircraft(data, lat, lon, radius_mi):
    """readsb JSON ({"ac": [...]}, or "aircraft" in older versions) -> planes in the air
    within the radius, closest first, each with dist_mi."""
    out = []
    for a in _rows(data):
        p = parse_one(a)
        if not p or p["ground"] or p["alt_ft"] < 100:
            continue
        p["dist_mi"] = distance_mi(lat, lon, p["lat"], p["lon"])
        if p["dist_mi"] <= radius_mi:
            out.append(p)
    out.sort(key=lambda p: p["dist_mi"])
    return out


def parse_route(data):
    """adsbdb /callsign -> {orig, dest, orig_city, dest_city, orig_pos, dest_pos, airline,
    airline_icao, airline_iata}, or None if unknown or unusable."""
    resp = (data or {}).get("response") if isinstance(data, dict) else None
    fr = resp.get("flightroute") if isinstance(resp, dict) else None
    if not isinstance(fr, dict):
        return None

    def ap(side):
        x = fr.get(side)
        if not isinstance(x, dict):
            return "", "", None
        code = str(x.get("iata_code") or x.get("icao_code") or "").strip().upper()
        lat, lon = _num(x.get("latitude")), _num(x.get("longitude"))
        pos = (lat, lon) if lat is not None and lon is not None else None
        return (code if re.fullmatch(r"[A-Z0-9]{3,4}", code) else ""), str(x.get("municipality") or "").strip()[:30], pos

    orig, ocity, opos = ap("origin")
    dest, dcity, dpos = ap("destination")
    if not orig or not dest:
        return None
    al = fr.get("airline") if isinstance(fr.get("airline"), dict) else {}
    icao = str(al.get("icao") or "").strip().upper()
    iata = str(al.get("iata") or "").strip().upper()
    return {"orig": orig, "dest": dest, "orig_city": ocity, "dest_city": dcity,
            "orig_pos": opos, "dest_pos": dpos,
            "airline": str(al.get("name") or "").strip()[:40],
            "airline_icao": icao if re.fullmatch(r"[A-Z]{3}", icao) else "",
            "airline_iata": iata if re.fullmatch(r"[A-Z0-9]{2}", iata) else ""}


def parse_place(data):
    """Nominatim reverse result -> 'Austin, TX'-style text, or '' (e.g. over open water)."""
    if not isinstance(data, dict) or data.get("error"):
        return ""
    addr = data.get("address") if isinstance(data.get("address"), dict) else {}
    town = next((addr.get(k) for k in ("city", "town", "village", "hamlet", "county") if addr.get(k)), "")
    region = addr.get("state") or addr.get("region") or ""
    code = str(addr.get("ISO3166-2-lvl4") or "")
    if addr.get("country_code") == "us" and code.startswith("US-"):
        region = code[3:]                                  # Texas -> TX
    elif addr.get("country_code") not in (None, "us"):
        region = region or addr.get("country", "")
    town = re.sub(r" County$", " Co", str(town))
    text = ", ".join(x for x in (town, region) if x) or str(addr.get("country") or "")
    return re.sub(r"\s+", " ", text).strip()[:40]


def parse_flight_number(text):
    """'aa 1234', 'AA1234', 'AAL1234', 'ENY3456' -> 'AA1234' / 'AAL1234' (spaces and
    dashes dropped). Raises ValueError for anything that isn't a flight number."""
    t = re.sub(r"[\s\-]", "", str(text or "")).upper()
    m = FLIGHT_RE.fullmatch(t)
    if not m or (len(m[1]) == 2 and m[1].isdigit()):
        raise ValueError(f"{str(text)[:20]!r} isn't a flight number (e.g. AA1234)")
    return t


def parse_tracked(raw):
    """Phone page text (one per line or comma separated) -> canonical list."""
    out = []
    for part in re.split(r"[\n,;]+", raw or ""):
        if part.strip():
            f = parse_flight_number(part)
            if f not in out:
                out.append(f)
    if len(out) > MAX_TRACKED:
        raise ValueError(f"at most {MAX_TRACKED} flights")
    return out


def tracked_from_env(raw):
    out = []
    for part in (raw or "").split(","):
        try:
            f = parse_flight_number(part) if part.strip() else None
        except ValueError as exc:
            log.warning("ignoring tracked flight: %s", exc)
            continue
        if f and f not in out:
            out.append(f)
    return out[:MAX_TRACKED]


# --- shared helpers ------------------------------------------------------------------
class _Lookups:
    """Web requests and cached route/airline lookups (adsbdb), shared by the overhead and
    tracking feeds. Both run in their own background threads, so requests go through
    one lock: one at a time, spaced per host (a few a minute at most)."""

    def __init__(self, session):
        self.session = session
        self.routes = {}            # callsign -> (route or None, expires)
        self.airlines = {}          # IATA code -> ICAO code ("" if unknown)
        self.lock = threading.Lock()
        self.http_lock = threading.Lock()

    def get(self, url, **kw):
        with self.http_lock:
            _throttle(url)
            return self.session.get(url, timeout=kw.pop("timeout", 12), **kw)

    def route(self, callsign, now=None):
        with self.lock:
            hit = self.routes.get(callsign)
        return hit[0] if hit and hit[1] > (now or time.time()) else None

    def lookup_route(self, callsign, now):
        """Fetch and cache; returns True if a request was made."""
        with self.lock:
            hit = self.routes.get(callsign)
        if hit and hit[1] > now:
            return False
        try:
            r = self.get(ROUTE_URL.format(callsign), timeout=10)
            if r.status_code == 404:
                route = None
            else:
                r.raise_for_status()
                route = parse_route(r.json())
        except Exception as exc:  # left uncached: tried again next time
            log.info("flights: route for %s unavailable (%s)", callsign, str(exc)[:120])
            return True
        with self.lock:
            self.routes[callsign] = (route, now + (ROUTE_TTL if route else ROUTE_MISS_TTL))
            if len(self.routes) > 500:
                self.routes = {k: v for k, v in self.routes.items() if v[1] > now}
        return True

    def seen_airlines(self):
        """{ICAO: (IATA, name)} for every airline in the route cache (for the phone page)."""
        with self.lock:
            routes = [v[0] for v in self.routes.values() if v[0]]
        return {r["airline_icao"]: (r.get("airline_iata", ""), r.get("airline", ""))
                for r in routes if r.get("airline_icao")}

    def icao_for(self, iata):
        if iata in IATA_ICAO:
            return IATA_ICAO[iata]
        with self.lock:
            if iata in self.airlines:
                return self.airlines[iata]
        icao = ""
        try:
            r = self.get(AIRLINE_URL.format(iata), timeout=10)
            if r.status_code != 404:
                r.raise_for_status()
                rows = (r.json() or {}).get("response")
                rows = rows if isinstance(rows, list) else [rows] if isinstance(rows, dict) else []
                icao = next((str(x.get("icao", "")).upper() for x in rows if isinstance(x, dict)
                             and str(x.get("iata", "")).upper() == iata
                             and re.fullmatch(r"[A-Z]{3}", str(x.get("icao", "")).upper())), "")
        except Exception as exc:
            log.info("flights: airline %s unavailable (%s)", iata, str(exc)[:120])
            return ""  # not cached: try again later
        with self.lock:
            self.airlines[iata] = icao
        return icao

    def callsign_for(self, flight):
        """'AA1234' -> 'AAL1234'; an ICAO callsign stays as it is; '' if unknown yet."""
        m = FLIGHT_RE.fullmatch(flight)
        if not m:
            return ""
        prefix, num = m[1], m[2]
        if len(prefix) == 3 and prefix.isalpha():
            return flight
        icao = self.icao_for(prefix)
        return f"{icao}{num}" if icao else ""


def airline_codes(callsign, route=None):
    """(ICAO, IATA) for the badge: from the route when known, else the callsign prefix."""
    route = route or {}
    icao = route.get("airline_icao") or (callsign[:3] if AIRLINE_CALLSIGN.fullmatch(callsign or "") else "")
    iata = route.get("airline_iata") or next((k for k, v in IATA_ICAO.items() if v == icao), "")
    return icao, iata


# --- planes overhead ------------------------------------------------------------------
class Flights(Feed):
    refresh_every = 30

    def __init__(self, lat, lon, radius_mi=10, lookups=None):
        super().__init__()
        self.lat, self.lon = lat, lon
        self.radius_mi = radius_mi
        self.lookups = lookups or _Lookups(self.session)
        self.source = ""
        self.fetched = 0.0

    def _positions(self):
        nm = max(1, math.ceil(self.radius_mi / MI_PER_NM))
        last = None
        for name, url in POINT_SOURCES:
            try:
                r = self.lookups.get(url.format(lat=self.lat, lon=self.lon, nm=nm))
                r.raise_for_status()
                planes = parse_aircraft(r.json(), self.lat, self.lon, self.radius_mi)
                self.source = name
                return planes
            except Exception as exc:
                last = exc
                log.info("flights: %s unavailable (%s)", name, str(exc)[:120])
        raise last or ValueError("no flight source")

    def route(self, callsign, now=None):
        return self.lookups.route(callsign, now)

    def fetch(self):
        if self.lat is None or self.lon is None:
            return None
        planes = self._positions()
        now = time.time()
        asked = 0
        for p in planes:
            if asked >= ROUTE_LOOKUPS_PER_FETCH:
                break
            if AIRLINE_CALLSIGN.fullmatch(p["callsign"]):
                asked += self.lookups.lookup_route(p["callsign"], now)
        self.fetched = now
        return planes

    def overhead(self, n, now=None):
        """The n closest planes with their routes, or [] if the data is too old to show."""
        now = now or time.time()
        if not self.data or now - self.fetched > MAX_AGE:
            return []
        return [{**p, "route": self.route(p["callsign"], now)} for p in self.data[:n]]


# --- tracked flights -----------------------------------------------------------------
class Tracker(Feed):
    """Follows flight numbers. Each entry's state: waiting (not seen in the air yet),
    ground (seen on the ground before departing), air, lost (dropped out mid-flight),
    landed. Saved to cache/ so LANDED survives a restart."""
    refresh_every = 60

    def __init__(self, flights, lookups=None, state_path=STATE_PATH):
        super().__init__()
        self.flights = list(flights)
        self.lookups = lookups or _Lookups(self.session)
        self.state_path = state_path
        self.lock = threading.Lock()
        self.state = self._load()

    # persistence
    def _load(self):
        try:
            with open(self.state_path) as fh:
                data = json.load(fh)
            return {k: v for k, v in data.items() if k in self.flights and isinstance(v, dict)}
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.info("flights: couldn't save tracked state (%s)", exc)

    # fetching
    def _position(self, callsign):
        """Live readsb row for a callsign (None if no receiver hears it)."""
        last = None
        for name, url in CALLSIGN_SOURCES:
            try:
                r = self.lookups.get(url.format(callsign))
                r.raise_for_status()
                rows = [p for p in (parse_one(a) for a in _rows(r.json())) if p and p["callsign"] == callsign]
                rows.sort(key=lambda p: p["ground"])          # an airborne match beats a stale ground one
                return rows[0] if rows else None
            except Exception as exc:
                last = exc
                log.info("flights: %s unavailable (%s)", name, str(exc)[:120])
        raise last or ValueError("no flight source")

    def _place(self, lat, lon):
        url = REVERSE_URL
        try:
            _throttle(url)
            r = self.session.get(url, params={"lat": f"{lat:.3f}", "lon": f"{lon:.3f}", "format": "jsonv2",
                                              "zoom": 10, "addressdetails": 1},
                                 headers={"User-Agent": GEOCODE_UA, "Accept-Language": "en"}, timeout=10)
            r.raise_for_status()
            return parse_place(r.json())
        except Exception as exc:
            log.info("flights: place lookup unavailable (%s)", str(exc)[:120])
            return None  # unknown: keep the last place

    def update_one(self, flight, now):
        with self.lock:
            st = dict(self.state.get(flight) or {"status": "waiting"})
        cs = st.get("callsign") or self.lookups.callsign_for(flight)
        if not cs:
            st.update(status="waiting", error="airline not found")
            with self.lock:
                self.state[flight] = st
            return
        st["callsign"] = cs
        st.pop("error", None)
        self.lookups.lookup_route(cs, now)
        route = self.lookups.route(cs, now)
        p = self._position(cs)
        status = st.get("status", "waiting")
        if status == "landed" and now - st.get("landed_at", now) > LANDED_SHOW:
            status = "waiting"
            for k in ("landed_at", "last", "place", "place_at", "seen_at"):
                st.pop(k, None)
        if status == "lost" and now - st.get("seen_at", now) > LOST_FORGET:
            status = "waiting"
        if p is not None:
            if p["ground"]:
                if status in ("air", "lost"):
                    status, st["landed_at"] = "landed", now
                elif status != "landed":
                    status = "ground"
            elif status != "landed" or now - st.get("landed_at", now) > 1800:
                status = "air"                            # (a landed flight seen flying again 30+ min later: next trip)
                st.pop("landed_at", None)
            if status in ("air", "ground"):
                st["last"] = p
                st["seen_at"] = now
                if status == "air" and now - st.get("place_at", 0) > PLACE_EVERY:
                    place = self._place(p["lat"], p["lon"])
                    if place is not None:
                        st["place"], st["place_at"] = place, now
        elif status == "ground" and now - st.get("seen_at", now) > 2 * 3600:
            status = "waiting"                             # parked and gone quiet: wait for the trip
        elif status == "air":
            last, seen = st.get("last") or {}, st.get("seen_at", now)
            dest = (route or {}).get("dest_pos")
            near = dest and last.get("lat") is not None and \
                distance_mi(last["lat"], last["lon"], *dest) <= LANDING_DIST_MI
            low = last.get("alt_ft", 99999) <= LANDING_ALT_FT
            if low and (near or not dest) and now - seen > 3 * 60:
                status, st["landed_at"] = "landed", seen
            elif now - seen > LOST_AFTER:
                status = "lost"
        st["status"] = status
        with self.lock:
            self.state[flight] = st

    def fetch(self):
        now = time.time()
        ok = 0
        for f in self.flights:
            try:
                self.update_one(f, now)
                ok += 1
            except Exception as exc:  # one flight failing never blanks the others
                log.info("flights: tracking %s failed (%s)", f, str(exc)[:120])
                with self.lock:
                    self.state.setdefault(f, {"status": "waiting"})["error"] = str(exc)[:80]
        with self.lock:
            self.state = {k: v for k, v in self.state.items() if k in self.flights}
            self._save()
        if self.flights and not ok:
            raise ValueError("no tracking data")
        return now

    # display
    def rows(self, now=None):
        """One dict per tracked flight, in the order entered, ready to draw."""
        now = now or time.time()
        out = []
        with self.lock:
            items = [(f, dict(self.state.get(f) or {"status": "waiting"})) for f in self.flights]
        for flight, st in items:
            cs = st.get("callsign", "")
            route = self.lookups.route(cs, now) if cs else None
            last = st.get("last") or {}
            status = st.get("status", "waiting")
            if self.data is None and flight not in self.state:
                status = "checking"                        # first lookup still running
            if status == "air" and now - st.get("seen_at", 0) > MAX_AGE:
                status = "lost"                            # not refreshed lately: don't pretend it's live
            row = {"flight": flight, "callsign": cs, "status": status, "route": route,
                   "place": st.get("place", ""), "seen_at": st.get("seen_at"),
                   "landed_at": st.get("landed_at"), "error": st.get("error", ""),
                   "type": last.get("type", ""), "alt_ft": last.get("alt_ft"),
                   "speed_kt": last.get("speed_kt"), "vrate_fpm": last.get("vrate_fpm"),
                   "track": last.get("track"), "progress": None, "eta": None, "remaining_mi": None}
            if route and route.get("orig_pos") and route.get("dest_pos") and last.get("lat") is not None:
                total = distance_mi(*route["orig_pos"], *route["dest_pos"])
                left = distance_mi(last["lat"], last["lon"], *route["dest_pos"])
                if total > 1:
                    row["progress"] = max(0.0, min(1.0, 1 - left / total))
                row["remaining_mi"] = left
                kt = last.get("speed_kt")
                if status == "air" and kt and kt > 60:
                    row["eta"] = st.get("seen_at", now) + left / (kt * MI_PER_NM) * 3600
            if status == "landed":
                row["progress"] = 1.0
            out.append(row)
        return out

    def status_for(self):
        """For the phone page: per flight, a short status line."""
        return {r["flight"]: {"status": r["status"], "callsign": r["callsign"], "error": r["error"],
                              "route": (f"{r['route']['orig']}-{r['route']['dest']}" if r["route"] else ""),
                              "place": r["place"]} for r in self.rows()}
