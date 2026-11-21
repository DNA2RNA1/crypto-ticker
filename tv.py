"""TV dashboard: the same data as the LED ticker, drawn full-screen in a browser on a TV.

A Pi set up with DEVICE=tv has no LED panel. The ticker runs as usual with a display
that draws nothing (NullDisplay), so prices, weather, news, sports and flights keep
refreshing exactly as they do on a panel, and the Pi's own browser (Chromium in kiosk
mode, see install.sh --tv) shows http://localhost:<port>/tv, which reads /api/tv.

Which screens the hidden loop keeps fresh comes from the TV pages that are switched on
(TV_PAGES), not from SCREENS, which only means something on an LED panel.
"""

import math
import os
import time

from flights import distance_mi, guess_leg

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(HERE, "icons")
AIRLINE_DIR = os.path.join(ICON_DIR, "airlines")

TV_PAGES = ("flights", "flightmap", "tracked", "spotlight", "crypto", "chart", "weather", "news", "heatmap",
            "feargreed", "sunmoon", "scores", "lines", "arcade")
TV_PAGE_NAMES = {"flights": "Flights overhead", "flightmap": "Flight map", "tracked": "Tracked flights", "crypto": "Crypto overview",
                 "spotlight": "Coins one at a time", "chart": "Price charts", "weather": "Weather", "news": "Headlines",
                 "heatmap": "Crypto heatmap", "feargreed": "Fear & Greed", "sunmoon": "Sun & moon",
                 "scores": "Scores", "lines": "Betting lines", "arcade": "Arcade (chomper & ghosts)"}
DEFAULT_TV_PAGES = ("flights", "flightmap", "tracked", "spotlight", "crypto", "chart", "weather", "news", "heatmap",
                    "sunmoon", "scores", "arcade")
TV_MODES = ("cycle", "single")

# TV page -> the LED screen whose feed it needs (the hidden loop refreshes those)
_FEEDS = {"flights": "flights", "flightmap": "flights", "tracked": "flights", "spotlight": "coins", "crypto": "coins", "chart": "coins",
          "heatmap": "coins", "weather": "weather", "sunmoon": "weather", "news": "news",
          "feargreed": "feargreed", "scores": "sports", "lines": "sports"}


MAP_RADII = (1, 2, 5, 10, 15, 20, 25, 50, 75, 100)  # flight map: miles from the center to the outer ring
MAP_VIEW = 2.3                         # the TV is this many radii wide on each side of the center
MAP_MAX_PAN = 100                      # miles the map can be moved from home
MAP_MAX_PLANES = 150


def parse_pan(raw):
    """'3.5,-2' -> (3.5, -2.0): the map's center moved east and north of home, in miles."""
    try:
        e, n = (float(v) for v in str(raw or "").split(","))
    except ValueError:
        return (0.0, 0.0)
    if e != e or n != n:
        return (0.0, 0.0)
    return (max(-MAP_MAX_PAN, min(MAP_MAX_PAN, round(e, 1))), max(-MAP_MAX_PAN, min(MAP_MAX_PAN, round(n, 1))))


def parse_home(raw):
    """'26.30123,-98.16012' -> (26.30123, -98.16012); None when unset or not a place."""
    try:
        lat, lon = (float(v) for v in str(raw or "").split(","))
    except ValueError:
        return None
    return (round(lat, 5), round(lon, 5)) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def map_home(cfg):
    """Where the flight map is centered: its own home setting, else the weather location."""
    home = getattr(cfg, "tv_map_home", None)
    if home:
        return home
    lat, lon = getattr(cfg, "weather_lat", None), getattr(cfg, "weather_lon", None)
    return None if lat is None or lon is None else (lat, lon)


def map_on(cfg):
    return getattr(cfg, "device", "led") == "tv" and "flightmap" in (getattr(cfg, "tv_pages", None) or [])


def flights_fetch_radius(cfg):
    """How far around home to ask for planes: the Flights setting, or, with the TV's flight
    map on, enough to fill the screen wherever the map is moved (the sources allow 250 nm)."""
    r = cfg.flights_radius
    if map_on(cfg):
        e, n = cfg.tv_map_pan
        home, wx = map_home(cfg), (getattr(cfg, "weather_lat", None), getattr(cfg, "weather_lon", None))
        off = distance_mi(*home, *wx) if home and None not in wx else 0  # planes are asked for around the weather location
        r = max(r, math.ceil(cfg.tv_map_radius * MAP_VIEW + math.hypot(e, n) + off))
    return min(r, 280)


def screens_for(pages, mode="cycle"):
    """LED screen names whose data the TV needs. The single dashboard shows flights,
    coins, weather, scores and headlines whatever pages are switched on."""
    need = {_FEEDS[p] for p in pages if p in _FEEDS}
    if mode == "single":
        need |= {"flights", "coins", "weather", "sports", "news"}
    order = ("coins", "weather", "news", "sports", "flights", "feargreed")
    return [s for s in order if s in need]


def parse_pages(raw):
    """'crypto,chart,weather' -> known pages in that order (unknown names dropped)."""
    out = []
    for p in (raw or "").split(","):
        p = p.strip().lower()
        if p in TV_PAGES and p not in out:
            out.append(p)
    return out


class NullDisplay:
    """A display that draws nothing: the TV Pi has no panel. show() just waits like a
    real panel would, so the ticker's loop keeps its normal pace (and feeds refresh)."""
    realtime = True

    def __init__(self, width=64, height=32):
        self.width, self.height = width, height
        self.wake = None

    def show(self, image, hold=0.0):
        if hold > 0:
            if self.wake is not None:
                self.wake.wait(hold)
            else:
                time.sleep(hold)

    def set_brightness(self, value):
        pass

    def close(self):
        pass


_arcade = []   # the panel's arcade scenes, built once
_sheets = {}


def _scenes():
    if not _arcade:
        import arcade
        _arcade.extend(make() for make in arcade.SCENES)
    return _arcade


def arcade_list():
    """The arcade scenes the TV can play: [{name, fps, frames}]."""
    return [{"name": a.name, "fps": a.fps, "frames": len(a.frames)} for a in _scenes()]


def arcade_sheet(i):
    """Scene i as one PNG: its 64x32 frames stacked top to bottom (the TV draws them big)."""
    import io
    from PIL import Image
    scenes = _scenes()
    if not 0 <= i < len(scenes):
        raise IndexError(i)
    if i not in _sheets:
        frames = scenes[i].frames
        w, h = frames[0].size
        sheet = Image.new("RGB", (w, h * len(frames)))
        for k, f in enumerate(frames):
            sheet.paste(f.convert("RGB"), (0, h * k))
        buf = io.BytesIO()
        sheet.save(buf, "PNG", optimize=True)
        _sheets[i] = buf.getvalue()
    return _sheets[i]


def candles_4h(points):
    """7 days of hourly prices -> 4-hour candles [open, high, low, close], oldest first."""
    pts = [p for p in points or [] if isinstance(p, (int, float))]
    out = []
    for i in range(0, len(pts) - 1, 4):
        chunk = pts[i:i + 5]
        if len(chunk) >= 2:
            out.append([chunk[0], max(chunk), min(chunk), chunk[-1]])
    return out


def coin_image(a):
    """The coin's picture for the TV: one you uploaded on the phone page (icons/<sym>.png)
    wins; otherwise the coin's official logo from the price source."""
    sym = str(a.get("symbol") or "").lower()
    path = os.path.join(ICON_DIR, sym + ".png")
    if sym and os.path.exists(path):
        return f"/icons/{sym}.png?v={int(os.path.getmtime(path))}"
    return a.get("image")


def airline_icons():
    """Airlines you gave your own picture (phone page): {ICAO: url}."""
    out = {}
    try:
        for name in os.listdir(AIRLINE_DIR):
            if len(name) == 7 and name.endswith(".png") and name[:3].isalpha() and name[:3].isupper():
                out[name[:3]] = f"/icons/airlines/{name}?v={int(os.path.getmtime(os.path.join(AIRLINE_DIR, name)))}"
    except OSError:
        pass
    return out


def build():
    """Changes when the TV page's code does (after an Update), so the TV reloads itself."""
    try:
        return int(max(os.path.getmtime(os.path.join(HERE, f)) for f in ("tv.html", "tv.py")))
    except OSError:
        return 0


def _asset(a):
    spark = a.get("sparkline") or []
    return {"symbol": a.get("symbol"), "name": a.get("name"), "price": a.get("price"),
            "change": a.get("change_24h"), "market_cap": a.get("market_cap"),
            "image": coin_image(a),
            "spark": spark[::4] if len(spark) > 60 else spark,
            "candles": candles_4h(spark)}


def _game(g, odds=False):
    def team(t):
        return {k: t.get(k) for k in ("abbr", "name", "color", "alt", "score", "record", "winner")}
    return {"league": g.get("league"), "state": g.get("state"), "start": g.get("start"),
            "detail": g.get("detail"), "away": team(g["away"]), "home": team(g["home"]),
            "odds": g.get("odds") if odds else None}  # pre-game line (ESPN), display only


def flight_map(ticker, now):
    """The flight map page: where it's centered, how far it reaches, and every plane the
    feed has (the page draws the ones on screen), with routes already looked up."""
    cfg = ticker.cfg
    home = map_home(cfg)
    if home is None:
        return None
    f = ticker.flights
    planes = []
    for p in f.overhead(MAP_MAX_PLANES, now):
        r = p.get("route") or {}
        planes.append({k: p.get(k) for k in ("hex", "callsign", "reg", "type", "lat", "lon", "track",
                                             "alt_ft", "speed_kt", "vrate_fpm", "dist_mi")}
                      | {"route": {k: r.get(k) for k in ("orig", "dest", "orig_pos", "dest_pos")} if r else None,
                         "guess": None if r else guess_leg(p)})
    return {"lat": home[0], "lon": home[1], "radius": cfg.tv_map_radius,
            "pan": list(cfg.tv_map_pan), "style": cfg.tv_map_style,
            "fetched": f.fetched or None, "planes": planes}


def payload(ticker, now=None):
    """Everything the TV page draws, as JSON-ready data. Asking for it also nudges the
    feeds the switched-on pages use (each refreshes on its own schedule; nothing waits)."""
    cfg = ticker.cfg
    now = now or time.time()
    pages = list(cfg.tv_pages)
    need = set(screens_for(pages, cfg.tv_mode))
    if "weather" in need:
        ticker.weather.update()
    if "feargreed" in need:
        ticker.fear_greed.update()
    if "news" in need:
        ticker.news.update()
    if "sports" in need:
        ticker.sports.update()
    if "flights" in need:
        if cfg.flights_track:
            ticker.tracker.update()
        if cfg.flights_overhead or "flightmap" in pages:
            ticker.flights.update()

    def safe(fn, default):
        try:
            return fn()
        except Exception:  # one odd feed never blanks the TV
            return default

    # betting lines: the phone's Betting lines switch, or the Betting lines page being on
    odds = bool(getattr(cfg, "sports_odds", False)) or "lines" in pages
    news = [{"title": n.get("title"), "source": n.get("source"), "at": n.get("at")}
            for n in (ticker.news.data or [])[:12]] if "news" in need else []
    return {
        "now": now,
        "build": build(),
        "tz": getattr(ticker.tz, "key", None),  # None: the Pi's own clock setting
        "place": cfg.weather_place,
        "unit": cfg.temp_unit,
        "currency": cfg.currency,
        "settings": {"mode": cfg.tv_mode, "pages": pages, "seconds": cfg.tv_seconds,
                     "popup": cfg.tv_flight_popup, "odds": bool(getattr(cfg, "sports_odds", False)),
                     "arcade": getattr(cfg, "tv_arcade_style", "led")},
        "stale": bool(ticker.stale),
        "assets": [_asset(a) for a in (ticker.assets or [])] if "coins" in need else [],
        "weather": ticker.weather.data if "weather" in need else None,
        "fear_greed": ticker.fear_greed.data if "feargreed" in need else None,
        "news": news,
        "games": safe(lambda: [_game(g, odds) for g in ticker.sports.games()][:16], []) if "sports" in need else [],
        "lines": safe(lambda: [dict(_game(g, True), fav=g.get("fav")) for g in ticker.sports.lines()], [])
        if odds and "sports" in need else [],
        "overhead": safe(lambda: ticker.flights.overhead(cfg.flights_per_cycle + 2, max_mi=cfg.flights_radius), [])
        if "flights" in need and cfg.flights_overhead else [],
        "tracked": safe(lambda: [dict(r, star=r["flight"] in (getattr(cfg, "flights_star", None) or [])) for r in ticker.tracker.rows()], [])
        if "flights" in need and cfg.flights_track else [],
        "airline_icons": airline_icons() if "flights" in need else {},
        "map": safe(lambda: flight_map(ticker, now), None) if "flightmap" in pages else None,
    }
