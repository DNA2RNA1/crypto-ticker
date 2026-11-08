"""Aircraft flying near you (the "flights" screen).

Uses the weather location as the center and shows the closest aircraft in the air:
callsign, route, airline, aircraft type, altitude and speed.

    positions   airplanes.live  /v2/point/<lat>/<lon>/<radius nm>   (no key, 1 request/s)
                adsb.fi         /api/v3/lat/<lat>/lon/<lon>/dist/<nm> (fallback, same format)
    routes      adsbdb.com      /v0/callsign/<CALLSIGN>              (no key)

All three are free for personal, non-commercial use. Positions refresh every 30 seconds,
and only while the flights screen is on. Routes are looked up once per callsign and
cached. Planes move, so old positions are never shown: if the data is more than two
minutes old (or there's nothing overhead) the screen is skipped.
"""

import logging
import math
import re
import threading
import time

from feeds import Feed

log = logging.getLogger("ticker.flights")

SOURCES = (
    ("airplanes.live", "https://api.airplanes.live/v2/point/{lat:.4f}/{lon:.4f}/{nm}"),
    ("adsb.fi", "https://opendata.adsb.fi/api/v3/lat/{lat:.4f}/lon/{lon:.4f}/dist/{nm}"),
)
ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{}"
RADIUS_CHOICES = (3, 5, 10, 15, 25)      # miles
MAX_AGE = 120                            # seconds before positions count as too old to show
ROUTE_TTL = 12 * 3600                    # a callsign's route, once found
ROUTE_MISS_TTL = 6 * 3600                # "unknown callsign", before asking again
ROUTE_LOOKUPS_PER_FETCH = 4              # closest planes first; the rest next time
AIRLINE_CALLSIGN = re.compile(r"[A-Z]{3}\d{1,4}[A-Z]{0,2}")  # AAL1234, SWA22, UAL7K (not N123AB)
CALLSIGN_OK = re.compile(r"[A-Z0-9]{2,8}")
MI_PER_NM = 1.15078
EARTH_MI = 3958.8


def distance_mi(lat1, lon1, lat2, lon2):
    """Great-circle distance in statute miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_MI * math.asin(min(1.0, math.sqrt(a)))


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_aircraft(data, lat, lon, radius_mi):
    """readsb-style JSON ({"ac": [...]}, or "aircraft" in older versions) -> planes in the
    air within the radius, closest first. Each: callsign, reg, type, alt_ft, speed_kt,
    vrate_fpm, track, dist_mi."""
    if not isinstance(data, dict):
        raise ValueError("unexpected flight data")
    rows = data.get("ac")
    if rows is None:
        rows = data.get("aircraft")
    if not isinstance(rows, list):
        raise ValueError("unexpected flight data")
    out = []
    for a in rows:
        if not isinstance(a, dict):
            continue
        alt = a.get("alt_baro")
        if alt == "ground" or a.get("ground"):
            continue
        alt = _num(alt if alt is not None else a.get("alt_geom"))
        plat, plon = _num(a.get("lat")), _num(a.get("lon"))
        if plat is None or plon is None or alt is None or alt < 100:
            continue
        dist = distance_mi(lat, lon, plat, plon)
        if dist > radius_mi:
            continue
        callsign = str(a.get("flight") or "").strip().upper()
        reg = str(a.get("r") or "").strip().upper()
        out.append({
            "callsign": callsign if CALLSIGN_OK.fullmatch(callsign) else "",
            "reg": reg[:10] if re.fullmatch(r"[A-Z0-9\-]{2,10}", reg) else "",
            "type": str(a.get("t") or "").strip().upper()[:6],
            "alt_ft": int(round(alt)),
            "speed_kt": _num(a.get("gs")),
            "vrate_fpm": _num(a.get("baro_rate") if a.get("baro_rate") is not None else a.get("geom_rate")),
            "track": _num(a.get("track")),
            "dist_mi": dist,
        })
    out.sort(key=lambda p: p["dist_mi"])
    return out


def parse_route(data):
    """adsbdb /callsign response -> {"orig", "dest", "orig_city", "dest_city", "airline"}
    or None if the callsign is unknown or the answer is unusable."""
    resp = (data or {}).get("response") if isinstance(data, dict) else None
    fr = resp.get("flightroute") if isinstance(resp, dict) else None
    if not isinstance(fr, dict):
        return None

    def ap(side):
        x = fr.get(side)
        if not isinstance(x, dict):
            return "", ""
        code = str(x.get("iata_code") or x.get("icao_code") or "").strip().upper()
        return (code if re.fullmatch(r"[A-Z0-9]{3,4}", code) else ""), str(x.get("municipality") or "").strip()[:30]

    orig, ocity = ap("origin")
    dest, dcity = ap("destination")
    if not orig or not dest:
        return None
    airline = fr.get("airline") if isinstance(fr.get("airline"), dict) else {}
    return {"orig": orig, "dest": dest, "orig_city": ocity, "dest_city": dcity,
            "airline": str(airline.get("name") or "").strip()[:40]}


class Flights(Feed):
    refresh_every = 30

    def __init__(self, lat, lon, radius_mi=10):
        super().__init__()
        self.lat, self.lon = lat, lon
        self.radius_mi = radius_mi
        self.routes = {}            # callsign -> (route dict or None, expires)
        self._lock = threading.Lock()
        self.source = ""            # which source answered last
        self.fetched = 0.0

    def _positions(self):
        nm = max(1, math.ceil(self.radius_mi / MI_PER_NM))
        last = None
        for name, url in SOURCES:
            try:
                r = self.session.get(url.format(lat=self.lat, lon=self.lon, nm=nm), timeout=12)
                r.raise_for_status()
                planes = parse_aircraft(r.json(), self.lat, self.lon, self.radius_mi)
                self.source = name
                return planes
            except Exception as exc:  # try the next source
                last = exc
                log.info("flights: %s unavailable (%s)", name, str(exc)[:120])
        raise last or ValueError("no flight source")

    def route(self, callsign, now=None):
        """Cached route for an airline callsign; None if unknown or not looked up yet."""
        with self._lock:
            hit = self.routes.get(callsign)
        return hit[0] if hit and hit[1] > (now or time.time()) else None

    def _lookup_routes(self, planes, now):
        done = 0
        for p in planes:
            cs = p["callsign"]
            if not AIRLINE_CALLSIGN.fullmatch(cs):
                continue
            with self._lock:
                hit = self.routes.get(cs)
            if hit and hit[1] > now:
                continue
            if done >= ROUTE_LOOKUPS_PER_FETCH:
                break
            done += 1
            try:
                r = self.session.get(ROUTE_URL.format(cs), timeout=10)
                if r.status_code == 404:
                    route = None
                else:
                    r.raise_for_status()
                    route = parse_route(r.json())
            except Exception as exc:  # leave it uncached; try again next fetch
                log.info("flights: route for %s unavailable (%s)", cs, str(exc)[:120])
                continue
            with self._lock:
                self.routes[cs] = (route, now + (ROUTE_TTL if route else ROUTE_MISS_TTL))
                if len(self.routes) > 500:  # drop expired entries now and then
                    self.routes = {k: v for k, v in self.routes.items() if v[1] > now}

    def fetch(self):
        if self.lat is None or self.lon is None:
            return None
        planes = self._positions()
        now = time.time()
        self._lookup_routes(planes, now)
        self.fetched = now
        return planes

    def overhead(self, n, now=None):
        """The n closest planes with their routes, or [] if the data is too old to show."""
        now = now or time.time()
        if not self.data or now - self.fetched > MAX_AGE:
            return []
        return [{**p, "route": self.route(p["callsign"], now)} for p in self.data[:n]]
