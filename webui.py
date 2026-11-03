"""Phone-friendly settings page served by the ticker on your home network.

Open http://<pi-name>.local:8080 in Safari, then Share -> Add to Home Screen.
Protected by WEB_PIN from settings.env. Uses only the Python standard library.
"""

import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

log = logging.getLogger("ticker.web")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
ICON_DIR = os.path.join(HERE, "icons")
MAX_UPLOAD = 8 * 1024 * 1024
COIN_META = os.path.join(CACHE, "coins.json")

EDITABLE = {  # key -> validator returning the string to store
    "SYMBOLS": lambda v: _symbols(v),
    "LAYOUT": lambda v: _choice(v.strip().lower(), ("classic", "chart", "mix")),
    "TRANSITION": lambda v: _choice(v.strip().lower(), ("scroll", "slide", "none")),
    "BRIGHTNESS": lambda v: _int(v, 5, 100),
    "SLEEP": lambda v: _int(v, 2, 60),
    "DIM_HOURS": lambda v: _dim(v),
    "DIM_BRIGHTNESS": lambda v: _int(v, 1, 100),
    "CURRENCY": lambda v: _choice(v.strip().lower(), ("usd", "eur", "gbp", "cad", "aud", "jpy")),
    "REFRESH_RATE": lambda v: _refresh(v),
    "FEATURED": lambda v: _featured(v),
    "SCREENS": lambda v: _screens(v),
    "WEATHER_LAT": lambda v: _float(v, -90, 90),
    "WEATHER_LON": lambda v: _float(v, -180, 180),
    "WEATHER_PLACE": lambda v: _text(v, 60),
    "TEMP_UNIT": lambda v: _choice(v.strip().upper(), ("F", "C")),
    "TIMEZONE": lambda v: _tz(v),
    "INDICES_SOURCE": lambda v: _choice(v.strip().lower(), ("coingecko", "yahoo")),
    "INDICES_VIEW": lambda v: _choice(v.strip().lower(), ("percent", "both")),
    "NEWS_FEEDS": lambda v: _feeds(v),
    "NEWS_PER_CYCLE": lambda v: _int(v, 1, 10),
    "NEWS_SPEED": lambda v: _choice(v.strip().lower(), ("slow", "normal", "fast")),
    "FOCUS": lambda v: _choice(v.strip().lower(), ("on", "off")),
    "LIST_VIEW": lambda v: _choice(v.strip().lower(), ("off", "5", "4")),
    "LIST_PAGE_SECONDS": lambda v: _int(v, 2, 60),
    "VIDEOS_CHANNELS": lambda v: _channels(v),
    "VIDEOS_PER_CYCLE": lambda v: _int(v, 1, 10),
    "VIDEOS_MAX_AGE_DAYS": lambda v: _int(v, 1, 30),
    "VIDEOS_SKIP_SHORTS": lambda v: _choice(v.strip().lower(), ("on", "off")),
}

MAX_FAILS = 5
LOCKOUT_SECONDS = 300


# --- validation --------------------------------------------------------------
# Every value from the phone goes through one of these before it touches settings.env.
# Each returns the exact string to store, or raises ValueError with a short message
# that the page shows to the user. Nothing is written unless every field passes.
COIN_RE = re.compile(r"[a-z0-9.\-]{1,20}(:[a-z0-9][a-z0-9_.\-]{0,99})?")
SYMBOL_RE = re.compile(r"[a-z0-9.\-]{1,20}")
LABELS = {"SYMBOLS": "Coins", "LAYOUT": "Layout", "TRANSITION": "Transition", "BRIGHTNESS": "Brightness",
          "SLEEP": "Time per coin", "DIM_HOURS": "Dim hours", "DIM_BRIGHTNESS": "Night brightness",
          "CURRENCY": "Currency", "REFRESH_RATE": "Refresh rate", "FEATURED": "Featured coins",
          "SCREENS": "Screens", "WEATHER_LAT": "Latitude", "WEATHER_LON": "Longitude",
          "WEATHER_PLACE": "Place name", "TEMP_UNIT": "Temperature unit", "TIMEZONE": "Time zone",
          "INDICES_SOURCE": "Indices source", "INDICES_VIEW": "Index change view", "FOCUS": "Focus", "LIST_VIEW": "List view",
          "LIST_PAGE_SECONDS": "Seconds per page", "NEWS_FEEDS": "News feeds",
          "NEWS_PER_CYCLE": "Headlines per cycle", "NEWS_SPEED": "Scroll speed",
          "VIDEOS_CHANNELS": "YouTube channels", "VIDEOS_PER_CYCLE": "Videos per cycle",
          "VIDEOS_MAX_AGE_DAYS": "Videos from the last", "VIDEOS_SKIP_SHORTS": "Skip Shorts"}


def _int(v, lo, hi):
    v = str(v).strip()
    if not re.fullmatch(r"-?\d{1,6}", v):
        raise ValueError("must be a whole number")
    n = int(v)
    if not lo <= n <= hi:
        raise ValueError(f"must be {lo}-{hi}")
    return str(n)


def _choice(v, options):
    if v not in options:
        raise ValueError(f"must be one of {', '.join(options)}")
    return v


def _dim(v):
    if v in ("", None):
        return ""
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", v.strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 23:
        raise ValueError("use start-end hours, e.g. 22-7")
    if int(m[1]) == int(m[2]):
        raise ValueError("start and end hours must differ")
    return f"{int(m[1])}-{int(m[2])}"


def _float(v, lo, hi):
    v = str(v).strip()
    if not re.fullmatch(r"-?\d{1,3}(\.\d{1,10})?", v):
        raise ValueError("must be a number")
    n = float(v)
    if not lo <= n <= hi:
        raise ValueError(f"must be {lo} to {hi}")
    return f"{n:.4f}"


def _text(v, maxlen):
    v = re.sub(r"\s", " ", str(v))
    v = re.sub(r"[^\w ,.'()-]", "", v, flags=re.UNICODE)
    return re.sub(r"\s+", " ", v).strip()[:maxlen]


def _tz(v):
    v = (v or "").strip()
    if not v:
        return ""
    if len(v) > 64 or not re.fullmatch(r"[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*", v):
        raise ValueError("unknown time zone")
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(v)
    except Exception:  # ZoneInfoNotFoundError is a KeyError, not a ValueError
        raise ValueError(f"unknown time zone {v!r}") from None
    return v


SCREENS = ("clock", "weather", "feargreed", "indices", "news", "videos", "sports", "coins")


def _screens(v):
    allowed = SCREENS
    names = []
    for x in (n.strip().lower() for n in v.split(",")):
        if not x:
            continue
        if x not in allowed:
            raise ValueError(f"unknown screen: {x[:20]}")
        if x not in names:
            names.append(x)
    if "coins" not in names:
        names.append("coins")
    return ",".join(names)


def _featured(v):
    syms = []
    for e in (x.strip().lower() for x in v.split(",")):
        if not e:
            continue
        if not SYMBOL_RE.fullmatch(e):
            raise ValueError(f"bad featured coin: {e[:20]}")
        if e not in syms:
            syms.append(e)
    if len(syms) > 30:
        raise ValueError("30 coins max")
    return ",".join(syms)


def _symbols(v):
    entries = []
    for e in (x.strip().lower() for x in v.split(",")):
        if not e:
            continue
        if not COIN_RE.fullmatch(e):
            raise ValueError(f"bad coin entry: {e[:40]}")
        if e not in entries:
            entries.append(e)
    if not entries:
        raise ValueError("add at least one coin")
    if len(entries) > 30:
        raise ValueError("30 coins max")
    return ",".join(entries)


def _feeds(v):
    """Feed URLs, one per line (or comma separated) -> comma-separated list."""
    urls = []
    for u in re.split(r"[\s,]+", str(v).strip()):
        if not u:
            continue
        if len(u) > 300 or not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[^\s,<>\"']*)?", u):
            raise ValueError(f"not a feed URL: {u[:60]}")
        if u not in urls:
            urls.append(u)
    if not urls:
        raise ValueError("add at least one feed URL")
    if len(urls) > 8:
        raise ValueError("8 feeds max")
    return ",".join(urls)


def _favorites(v):
    """ESPN team favorites "id:ABBR,id:ABBR" (ids are numbers)."""
    out = []
    for part in (p.strip() for p in str(v).split(",")):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,10}):([A-Za-z0-9&.'-]{1,6})", part)
        if not m:
            raise ValueError(f"bad team: {part[:20]}")
        item = f"{m[1]}:{m[2].upper()}"
        if item not in out:
            out.append(item)
    if len(out) > 20:
        raise ValueError("20 favorites max")
    return ",".join(out)


def _channels(v):
    """YouTube channel links / @handles / ids, one per line -> canonical, comma separated.
    Empty is allowed (no channels yet)."""
    from videos import parse_entries
    return ",".join(parse_entries(v))


def _default_feeds():
    from news import DEFAULT_FEEDS
    return DEFAULT_FEEDS


def _sports_keys():
    from sports import LEAGUES, TOP_CHOICES
    keys = {}
    for lg in LEAGUES:
        k = lg.key.upper()
        keys[f"SPORTS_{k}"] = lambda v: _choice(v.strip().lower(), ("on", "off"))
        choices = tuple(str(n) for n in TOP_CHOICES[lg.rankings])
        keys[f"SPORTS_{k}_TOP"] = lambda v, c=choices: _choice(str(v).strip(), c)
        keys[f"SPORTS_{k}_FAVORITES"] = lambda v: _favorites(v)
        LABELS[f"SPORTS_{k}"] = lg.name
        LABELS[f"SPORTS_{k}_TOP"] = f"{lg.name} top teams"
        LABELS[f"SPORTS_{k}_FAVORITES"] = f"{lg.name} favorites"
    return keys


EDITABLE.update(_sports_keys())


def _refresh(v):
    v = str(v).strip().lower()
    if v in ("auto", "0", ""):
        return "auto"
    return _int(v, 60, 3600)


def _coins_meta(items):
    """Names/icons of coins picked in search (display only). Bad entries are dropped."""
    if not isinstance(items, list):
        raise ValueError("bad coin details")
    out = []
    for c in items[:50]:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str):
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.\-]{0,99}", c["id"]):
            continue
        thumb = c.get("thumb") or c.get("image") or ""
        if not (isinstance(thumb, str) and thumb.startswith("https://") and len(thumb) < 500):
            thumb = ""
        out.append({"id": c["id"], "name": _text(c.get("name") or "", 60),
                    "symbol": _text(c.get("symbol") or "", 20), "thumb": thumb})
    return out


# --- settings.env read/write ---------------------------------------------------
def read_settings(path):
    out = {}
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_settings(path, updates):
    """Update KEY=value lines in place (keeping comments); append new keys."""
    for key, value in updates.items():  # last line of defence against injecting lines
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or re.search(r"[\r\n\x00]", str(value)):
            raise ValueError(f"refusing to write {key!r}")
    lines = []
    if os.path.exists(path):
        with open(path) as fh:
            lines = fh.read().splitlines()
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in updates:
            lines[i] = f"{key}={updates[key]}"
            done.add(key)
    for key, value in updates.items():
        if key not in done:
            lines.append(f"{key}={value}")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    if os.path.exists(path):  # keep the owner so you can still edit it by hand
        st = os.stat(path)
        os.chmod(tmp, st.st_mode & 0o777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except (PermissionError, AttributeError):
            pass
    os.replace(tmp, path)


# --- app state ---------------------------------------------------------------
class WebApp:
    def __init__(self, env_path, ticker=None, search=None, updater=None):
        self.env_path = env_path
        self.ticker = ticker
        self.updater = updater
        self.search_fn = search  # callable(query) -> list of coins
        self.fails = {}  # ip -> (count, locked_until)
        os.makedirs(CACHE, exist_ok=True)
        secret_path = os.path.join(CACHE, "web_secret")
        if not os.path.exists(secret_path):
            with open(secret_path, "w") as fh:
                fh.write(secrets.token_hex(32))
            os.chmod(secret_path, 0o600)
        with open(secret_path) as fh:
            self.secret = fh.read().strip().encode()

    # auth: the cookie is an HMAC of the PIN, so changing WEB_PIN logs everyone out
    def pin(self):
        return read_settings(self.env_path).get("WEB_PIN", "")

    def token(self):
        return hmac.new(self.secret, self.pin().encode(), hashlib.sha256).hexdigest()

    def authed(self, cookie_header):
        if not self.pin():
            return True
        c = SimpleCookie(cookie_header or "")
        return "tk" in c and hmac.compare_digest(c["tk"].value, self.token())

    def login(self, ip, pin):
        count, until = self.fails.get(ip, (0, 0))
        if time.time() < until:
            return False, f"Too many tries. Wait {int(until - time.time())}s."
        if not isinstance(pin, (str, int)) or len(str(pin)) > 64:
            pin = ""
        expected = self.pin()
        if expected and hmac.compare_digest(str(pin).encode(), expected.encode()):
            self.fails.pop(ip, None)
            return True, ""
        count += 1
        until = time.time() + LOCKOUT_SECONDS if count >= MAX_FAILS else 0
        self.fails[ip] = (0 if until else count, until)
        return False, "Wrong PIN." if not until else f"Locked for {LOCKOUT_SECONDS // 60} min."

    # coin names/icons for the list
    def _meta(self):
        try:
            with open(COIN_META) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def remember_coins(self, coins):
        meta = self._meta()
        if len(meta) > 500:  # keep the file small
            meta = {}
        for c in coins:
            if c.get("id"):
                meta[c["id"]] = {"name": c.get("name"), "symbol": c.get("symbol"),
                                 "image": c.get("thumb") or c.get("image")}
        with open(COIN_META, "w") as fh:
            json.dump(meta, fh)

    def settings(self):
        s = read_settings(self.env_path)

        def clean(key, default):
            """The stored value if it passes the same check as a save, else the default,
            so one bad hand edit in settings.env can't make every later save fail."""
            raw = s.get(key, "")
            if raw == "":
                return default
            try:
                return EDITABLE[key](raw)
            except ValueError:
                log.warning("settings.env has an invalid %s=%r; the page shows %r", key, raw, default)
                return default

        meta = self._meta()
        live = {a["symbol"].lower(): a for a in (self.ticker.assets if self.ticker else [])}
        coins = []
        for entry in (s.get("SYMBOLS") or "btc,eth").split(","):
            entry = entry.strip().lower()
            if not COIN_RE.fullmatch(entry):
                continue
            sym, _, cid = entry.partition(":")
            a = live.get(sym, {})
            m = meta.get(cid or a.get("id", ""), {})
            custom = os.path.exists(os.path.join(ICON_DIR, f"{sym}.png"))
            coins.append({"entry": entry, "symbol": sym.upper(),
                          "name": m.get("name") or a.get("name") or "",
                          "image": (f"/icons/{sym}.png?v={int(os.path.getmtime(os.path.join(ICON_DIR, sym + '.png')))}"
                                    if custom else m.get("image") or a.get("image") or ""),
                          "custom_icon": custom,
                          "price": a.get("price")})
        return {
            "coins": coins,
            "layout": clean("LAYOUT", "classic"),
            "transition": clean("TRANSITION", "scroll"),
            "brightness": int(clean("BRIGHTNESS", "70")),
            "sleep": int(clean("SLEEP", "5")),
            "dim_hours": clean("DIM_HOURS", ""),
            "dim_brightness": int(clean("DIM_BRIGHTNESS", "15")),
            "currency": clean("CURRENCY", "usd"),
            "refresh_rate": clean("REFRESH_RATE", "auto"),
            "usage": (self.ticker.usage.summary(self.ticker.interval()) if self.ticker else None),
            "pin_set": bool(s.get("WEB_PIN")),
            "featured": [x for x in clean("FEATURED", "").split(",") if x],
            "screens": clean("SCREENS", "clock,feargreed,indices,coins").split(","),
            "screen_names": list(SCREENS),
            "indices_source": clean("INDICES_SOURCE", "coingecko"),
            "indices_view": clean("INDICES_VIEW", "percent"),
            "weather_place": clean("WEATHER_PLACE", "") or ("Edinburg, Texas, US" if not s.get("WEATHER_LAT") else ""),
            "weather_lat": clean("WEATHER_LAT", "26.3017"),
            "weather_lon": clean("WEATHER_LON", "-98.1633"),
            "temp_unit": clean("TEMP_UNIT", "F"),
            "timezone": clean("TIMEZONE", ""),
            "focus": clean("FOCUS", "off"),
            "list_view": clean("LIST_VIEW", "off"),
            "list_page_seconds": int(clean("LIST_PAGE_SECONDS", "6")),
            "news_feeds": clean("NEWS_FEEDS", ",".join(_default_feeds())).split(","),
            "news_per_cycle": int(clean("NEWS_PER_CYCLE", "3")),
            "news_speed": clean("NEWS_SPEED", "normal"),
            "videos_channels": [c for c in clean("VIDEOS_CHANNELS", "").split(",") if c],
            "videos_per_cycle": int(clean("VIDEOS_PER_CYCLE", "3")),
            "videos_max_age_days": int(clean("VIDEOS_MAX_AGE_DAYS", "3")),
            "videos_skip_shorts": clean("VIDEOS_SKIP_SHORTS", "on"),
            "videos_status": self._videos_status(clean),
            "sports": self._sports(clean),
        }

    def _videos_status(self, clean):
        """Per channel: {"ok": True, "name"} once fetched, {"ok": False, "error"} if it
        failed, None if not checked yet (the screen off, or the first fetch pending)."""
        entries = [c for c in clean("VIDEOS_CHANNELS", "").split(",") if c]
        videos = getattr(self.ticker, "videos", None)
        if videos is None:
            return {e: None for e in entries}
        return videos.status_for(entries)

    def _sports(self, clean):
        """One entry per league for the Sports cards (out-of-season ones flagged)."""
        from sports import LEAGUES, TOP_CHOICES, in_season, parse_favorites
        out = []
        for lg in LEAGUES:
            k = lg.key.upper()
            favs = parse_favorites(clean(f"SPORTS_{k}_FAVORITES", ""))
            out.append({"key": lg.key, "name": lg.name, "in_season": in_season(lg), "rankings": lg.rankings,
                        "on": clean(f"SPORTS_{k}", "on" if lg.default_on else "off") == "on",
                        "top": int(clean(f"SPORTS_{k}_TOP", str(lg.default_top))),
                        "top_choices": list(TOP_CHOICES[lg.rankings]),
                        "favorites": [{"id": i, "abbr": a} for i, a in favs]})
        return out

    def team_search(self, league, query):
        sports = getattr(self.ticker, "sports", None)
        if sports is None:
            from sports import Sports
            sports = self._sports_client = getattr(self, "_sports_client", None) or Sports({})
        return sports.search_teams(league, query)

    def status(self):
        """Status section: price freshness, uptime, Wi-Fi, last error, software version."""
        import sysinfo
        return {
            "now": time.time(),
            "ticker": self.ticker.status() if self.ticker else None,
            "system_uptime": sysinfo.system_uptime(),
            "wifi": sysinfo.wifi(),
            "update": self.updater.status() if self.updater else None,
        }

    def save_icon(self, symbol, data):
        """Store an uploaded picture as icons/<symbol>.png (it overrides the default)."""
        if not SYMBOL_RE.fullmatch(symbol):
            raise ValueError("bad coin")
        if not data or len(data) > MAX_UPLOAD:
            raise ValueError("picture missing or too large")
        try:
            img = Image.open(io.BytesIO(data))
            if img.width * img.height > 40_000_000:  # ~40 MP; a Pi Zero has 512 MB of RAM
                raise ValueError("that picture is too big")
            img.load()
        except ValueError:
            raise
        except Exception:
            raise ValueError("that file isn't a picture I can read")
        img = img.convert("RGBA")
        img.thumbnail((256, 256))
        os.makedirs(ICON_DIR, exist_ok=True)
        img.save(os.path.join(ICON_DIR, f"{symbol}.png"))
        if self.ticker:
            self.ticker.request_reload()

    def reset_icon(self, symbol):
        path = os.path.join(ICON_DIR, f"{symbol}.png")
        if SYMBOL_RE.fullmatch(symbol) and os.path.exists(path):
            os.remove(path)
        if self.ticker:
            self.ticker.request_reload()

    def save(self, data):
        """Validate every field first; write settings.env only if all of them pass."""
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        updates = {}
        for key, check in EDITABLE.items():
            if key.lower() not in data:
                continue
            raw = data[key.lower()]
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                raise ValueError(f"{LABELS[key]}: wrong type")
            if len(str(raw)) > 2000:
                raise ValueError(f"{LABELS[key]}: too long")
            try:
                updates[key] = check(str(raw))
            except ValueError as exc:
                raise ValueError(f"{LABELS[key]}: {exc}") from None
        if "featured" in data and "SYMBOLS" in updates:  # featured coins must be on the list
            listed = {e.partition(":")[0] for e in updates["SYMBOLS"].split(",")}
            updates["FEATURED"] = ",".join(f for f in updates.get("FEATURED", "").split(",") if f in listed)
        meta = _coins_meta(data["coins_meta"]) if "coins_meta" in data else []
        write_settings(self.env_path, updates)
        if meta:
            self.remember_coins(meta)
        if self.ticker:
            self.ticker.request_reload()
        return updates

    def icon_png(self):
        """Home-screen icon: the current ticker screen, LED-style."""
        t = self.ticker
        img = None
        if t and t.assets:
            img = t.renderer._chart_screen(t.assets[0])
        if img is None:
            from render import Renderer
            img = Renderer(64, 32).message("$")
        big = img.resize((img.width * 3, img.height * 3), Image.NEAREST)
        out = Image.new("RGB", (192, 192), (0, 0, 0))
        out.paste(big, (0, (192 - big.height) // 2))
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()


# --- HTTP --------------------------------------------------------------------
def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "crypto-ticker"

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.client_address[0], *args)

        def _send(self, code, body, ctype="application/json", headers=None):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _length(self, limit):
            """Content-Length as an int, or raise ValueError/OverflowError."""
            raw = self.headers.get("Content-Length") or "0"
            if not raw.isdigit():
                raise ValueError("bad Content-Length")
            n = int(raw)
            if n > limit:
                raise OverflowError("request too large")
            return n

        def _body(self):
            n = self._length(65536)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, UnicodeDecodeError):
                raise ValueError("request isn't valid JSON") from None
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        def _authed(self):
            return app.authed(self.headers.get("Cookie"))

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if url.path in ("/icon.png", "/apple-touch-icon.png"):
                return self._send(200, app.icon_png(), "image/png")
            m = re.fullmatch(r"/icons/([a-z0-9.\-]+)\.png", url.path)
            if m and os.path.exists(os.path.join(ICON_DIR, m[1] + ".png")):
                with open(os.path.join(ICON_DIR, m[1] + ".png"), "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            if url.path == "/api/state":
                return self._send(200, {"authed": self._authed(), "pin_set": bool(app.pin())})
            if not self._authed():
                return self._send(401, {"error": "login required"})
            if url.path == "/api/settings":
                return self._send(200, app.settings())
            if url.path == "/api/status":
                return self._send(200, app.status())
            if url.path == "/api/geocode":
                q = parse_qs(url.query).get("q", [""])[0].strip()[:64]
                if len(q) < 3:
                    return self._send(200, {"places": []})
                try:
                    from feeds import geocode
                    return self._send(200, {"places": geocode(q)})
                except Exception as exc:
                    log.warning("geocode failed: %s", exc)
                    return self._send(502, {"error": "Location lookup failed. Check the Pi's internet."})
            if url.path == "/api/teams":
                qs = parse_qs(url.query)
                league = qs.get("league", [""])[0][:10]
                q = qs.get("q", [""])[0].strip()[:40]
                try:
                    return self._send(200, {"teams": app.team_search(league, q)})
                except ValueError as exc:
                    return self._send(400, {"error": str(exc)})
                except Exception as exc:
                    log.warning("team search failed: %s", exc)
                    return self._send(502, {"error": "Team search failed. Check the Pi's internet."})
            if url.path == "/api/search":
                q = parse_qs(url.query).get("q", [""])[0].strip()[:64]
                if len(q) < 2:
                    return self._send(200, {"coins": []})
                try:
                    return self._send(200, {"coins": app.search_fn(q) if app.search_fn else []})
                except Exception as exc:  # network trouble shouldn't kill the server
                    log.warning("search failed: %s", exc)
                    return self._send(502, {"error": "Search failed. Check the Pi's internet."})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            try:
                body = self._body() if url.path in ("/api/login", "/api/settings", "/api/update", "/api/rollback") else None
            except OverflowError as exc:
                return self._send(413, {"error": str(exc)})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            if url.path == "/api/login":
                ok, msg = app.login(self.client_address[0], body.get("pin", ""))
                if not ok:
                    return self._send(403, {"error": msg})
                cookie = f"tk={app.token()}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict"
                return self._send(200, {"ok": True}, headers={"Set-Cookie": cookie})
            if not self._authed():
                return self._send(401, {"error": "login required"})
            if url.path in ("/api/icon", "/api/icon/reset"):
                sym = parse_qs(url.query).get("sym", [""])[0].lower()
                try:
                    if url.path == "/api/icon":
                        try:
                            n = self._length(MAX_UPLOAD)
                        except OverflowError:
                            return self._send(413, {"error": "picture too large"})
                        app.save_icon(sym, self.rfile.read(n))
                    else:
                        app.reset_icon(sym)
                except ValueError as exc:
                    return self._send(400, {"error": str(exc)})
                return self._send(200, {"ok": True})
            if url.path in ("/api/update", "/api/rollback"):
                if not app.updater:
                    return self._send(404, {"error": "updates aren't available here"})
                from updater import UpdateError
                try:
                    if url.path == "/api/update":
                        app.updater.start_update()
                    else:
                        app.updater.start_rollback()
                except UpdateError as exc:
                    return self._send(409, {"error": str(exc)})
                return self._send(202, {"ok": True})
            if url.path == "/api/settings":
                try:
                    saved = app.save(body)
                except ValueError as exc:
                    return self._send(400, {"error": str(exc)})
                return self._send(200, {"ok": True, "saved": saved})
            self._send(404, {"error": "not found"})

    return Handler


def start(app, port=8080):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="webui").start()
    log.info("settings page on http://%s.local:%d", os.uname().nodename, port)
    return server


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Ticker">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<title>Ticker</title>
<style>
:root{--bg:#0b0d10;--card:#16191e;--line:#262a31;--text:#eef1f5;--muted:#8a93a0;--accent:#f5b400;--up:#1fd07a;--down:#ff4d3d}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.4 -apple-system,system-ui,sans-serif;
 padding:env(safe-area-inset-top) 16px calc(env(safe-area-inset-bottom) + 24px)}
h1{font-size:28px;margin:18px 0 4px}h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:26px 4px 8px}
.card{background:var(--card);border-radius:14px;overflow:hidden}
.row{display:flex;align-items:center;gap:12px;padding:12px 14px;border-top:1px solid var(--line);min-height:56px}
.row:first-child{border-top:0}
.row img,.ph{width:30px;height:30px;border-radius:50%;background:#222;flex:none}
.grow{flex:1;min-width:0}.sym{font-weight:700}.name{color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.btn{border:0;background:#232830;color:var(--text);width:38px;height:38px;border-radius:10px;font-size:17px}
.btn:disabled{opacity:.25}.del{color:var(--down)}.star{color:#5a616b;font-size:20px}.star.on{color:var(--accent)}
.pic{position:relative;flex:none;border:0;background:none;padding:0}.pic:after{content:"✎";position:absolute;right:-4px;bottom:-4px;font-size:11px;
 background:#2b313a;color:var(--text);border-radius:50%;width:16px;height:16px;line-height:16px;text-align:center}
.link{color:var(--accent);text-decoration:none}
input[type=search],input[type=password]{width:100%;padding:13px 14px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--text);font-size:17px}
.seg{display:flex;width:100%;background:#0f1216;border-radius:10px;padding:3px;gap:3px}
.seg button{flex:1;border:0;background:none;color:var(--muted);padding:9px 0;border-radius:8px;font-size:15px}
.seg button.on{background:#2b313a;color:var(--text);font-weight:600}
input[type=range]{width:100%;accent-color:var(--accent)}
select{background:#232830;color:var(--text);border:0;border-radius:8px;padding:8px;font-size:16px}
.val{color:var(--muted);font-variant-numeric:tabular-nums;min-width:48px;text-align:right}
.rank{color:var(--muted);font-size:13px}.add{color:var(--accent);font-size:26px;font-weight:300}
#toast{position:fixed;left:50%;bottom:calc(env(safe-area-inset-bottom) + 20px);transform:translateX(-50%);background:#2b313a;
 padding:10px 18px;border-radius:20px;font-size:15px;opacity:0;transition:opacity .25s;pointer-events:none}
#toast.show{opacity:1}.err{color:var(--down)}
.hint{color:var(--muted);font-size:13px;margin:8px 4px}
.chip{display:inline-flex;align-items:center;gap:6px;background:#232830;border-radius:16px;padding:5px 6px 5px 11px;font-weight:600;font-size:14px}
.chip button{border:0;background:#39404a;color:var(--down);border-radius:50%;width:22px;height:22px;font-size:12px;line-height:22px;padding:0}
.chip:first-letter{color:var(--accent)}
.login{max-width:320px;margin:22vh auto 0;text-align:center}
.login button{margin-top:12px;width:100%;padding:13px;border:0;border-radius:12px;background:var(--accent);color:#000;font-size:17px;font-weight:600}
.switch{position:relative;width:51px;height:31px;flex:none}.switch input{display:none}
.switch span{position:absolute;inset:0;background:#39404a;border-radius:31px;transition:.2s}
.switch span:before{content:"";position:absolute;width:27px;height:27px;left:2px;top:2px;background:#fff;border-radius:50%;transition:.2s}
.switch input:checked+span{background:var(--up)}.switch input:checked+span:before{transform:translateX(20px)}
</style></head><body>
<div id="app"></div><div id="toast"></div>
<script>
const $=s=>document.querySelector(s), app=$('#app');
let S=null, saveTimer=null, searchTimer=null, pendingMeta=[];
const esc=t=>String(t??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(msg,bad){const t=$('#toast');t.textContent=msg;t.className='show'+(bad?' err':'');clearTimeout(t._h);t._h=setTimeout(()=>t.className='',1600)}
async function api(path,body){
  const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
  const j=await r.json().catch(()=>({}));
  if(r.status==401){showLogin();throw new Error('login')}
  if(!r.ok)throw new Error(j.error||('HTTP '+r.status));
  return j;
}
function showLogin(msg){
  app.innerHTML=`<div class="login"><h1>Ticker</h1><p class="hint">Enter PIN</p>
  <input id="pin" type="password" inputmode="numeric" autocomplete="current-password" autofocus>
  <button id="go">Unlock</button><p class="err" id="lerr">${esc(msg||'')}</p></div>`;
  const go=async()=>{try{await api('/api/login',{pin:$('#pin').value});load()}catch(e){if(e.message!='login')$('#lerr').textContent=e.message}};
  $('#go').onclick=go;$('#pin').onkeydown=e=>{if(e.key=='Enter')go()};
}
async function load(){
  try{S=await api('/api/settings')}catch(e){return}
  render();
}
function coinRow(c,i,n){
  const img=c.image?`<img src="${esc(c.image)}" alt="">`:'<div class="ph"></div>';
  const sym=c.entry.split(':')[0], fav=(S.featured||[]).includes(sym);
  const sub=c.custom_icon?`<a href="#" class="link" data-reset="${i}">Reset picture</a>`:esc(c.name||c.entry);
  return `<div class="row"><button class="pic" data-pic="${i}" aria-label="Change icon">${img}</button>
  <div class="grow"><div class="sym">${esc(c.symbol)}</div><div class="name">${sub}</div></div>
  <button class="btn star ${fav?'on':''}" data-star="${i}" aria-label="Feature">★</button>
  <button class="btn" data-up="${i}" ${i==0?'disabled':''} aria-label="Move up">↑</button>
  <button class="btn" data-down="${i}" ${i==n-1?'disabled':''} aria-label="Move down">↓</button>
  <button class="btn del" data-del="${i}" aria-label="Remove">✕</button></div>`;
}
function seg(key,opts){return `<div class="seg" data-seg="${key}">${opts.map(([v,l])=>`<button data-v="${v}" class="${S[key]==v?'on':''}">${l}</button>`).join('')}</div>`}
function focusHint(){const h=$('#focushint');if(!h)return;
  const syms=S.coins.map(c=>c.entry.split(':')[0]);
  h.hidden=!(S.focus=='on'&&!(S.featured||[]).some(f=>syms.includes(f)))}
const SCREEN_LABELS={clock:'Clock',weather:'Weather',feargreed:'Fear &amp; Greed index',indices:'Market indices',
  news:'News headlines',videos:'YouTube videos',sports:'Sports scores'};
function screenOrder(){  // enabled screens in their saved order, then the others
  const all=(S.screen_names||Object.keys(SCREEN_LABELS)).filter(k=>k!='coins');
  const o=(S.screen_order||[]).filter(k=>all.includes(k));
  for(const k of S.screens)if(all.includes(k)&&!o.includes(k))o.push(k);
  for(const k of all)if(!o.includes(k))o.push(k);
  return S.screen_order=o;
}
function saveScreens(order,on){S.screens=[...order.filter(k=>on.has(k)),'coins'];save();render()}
function videosRows(){
  const st=S.videos_status||{};
  const line=c=>{const x=st[c];
    const msg=x==null?'<span class="name">Not checked yet</span>'
      :x.ok?`<span style="color:#3ad07a">✓ ${esc(x.name)}</span>`:`<span style="color:#ff6b5e">⚠ ${esc(x.error)}</span>`;
    return `<div class="row"><div class="grow" style="min-width:0"><div style="overflow:hidden;text-overflow:ellipsis;font:13px ui-monospace,monospace">${esc(c)}</div><div style="font-size:13px">${msg}</div></div></div>`};
  return `<h2>YouTube videos</h2><div class="card">
   <div class="row" style="display:block"><div>Channels<div class="name" style="white-space:normal">Paste channel links (youtube.com/@name), one per line. New uploads are checked every 30 minutes.</div></div>
    <textarea id="ytch" rows="${Math.max(3,(S.videos_channels||[]).length+1)}" wrap="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="https://www.youtube.com/@channel"
     style="width:100%;margin-top:8px;padding:10px;border-radius:10px;border:1px solid var(--line);background:#0f1216;color:var(--text);font:13px ui-monospace,monospace">${esc((S.videos_channels||[]).join('\n'))}</textarea></div>
   ${(S.videos_channels||[]).map(line).join('')}
   <div class="row"><div class="grow">Videos per cycle</div><select id="vper">${[1,2,3,4,5,6,8,10].map(n=>`<option ${S.videos_per_cycle==n?'selected':''}>${n}</option>`).join('')}</select></div>
   <div class="row"><div class="grow">Only videos from the last</div><select id="vage">${[1,2,3,5,7,14,30].map(n=>`<option value="${n}" ${S.videos_max_age_days==n?'selected':''}>${n} day${n>1?'s':''}</option>`).join('')}</select></div>
   <div class="row"><div class="grow">Skip Shorts</div><label class="switch"><input type="checkbox" id="vshorts" ${S.videos_skip_shorts!='off'?'checked':''}><span></span></label></div>
   <div class="row"><div class="name" style="white-space:normal">Titles scroll at the news screen's speed.</div></div>
  </div>`;
}
function newsRows(){
  return `<h2>News headlines</h2><div class="card">
   <div class="row" style="display:block"><div>Feeds<div class="name" style="white-space:normal">RSS or Atom URLs, one per line. Each is checked every 15 minutes.</div></div>
    <textarea id="feeds" rows="${Math.max(3,(S.news_feeds||[]).length)}" wrap="off" autocapitalize="off" autocorrect="off" spellcheck="false"
     style="width:100%;margin-top:8px;padding:10px;border-radius:10px;border:1px solid var(--line);background:#0f1216;color:var(--text);font:13px ui-monospace,monospace">${esc((S.news_feeds||[]).join('\n'))}</textarea></div>
   <div class="row"><div class="grow">Headlines per cycle</div><select id="nper">${[1,2,3,4,5,6,8,10].map(n=>`<option ${S.news_per_cycle==n?'selected':''}>${n}</option>`).join('')}</select></div>
   <div class="row"><div class="grow">Scroll speed</div></div>
   <div class="row" style="border-top:0;padding-top:0">${seg('news_speed',[['slow','Slow'],['normal','Normal'],['fast','Fast']])}</div>
  </div>`;
}
const lg=k=>S.sports.find(l=>l.key==k);
function sportsSection(){
  const on=(S.sports||[]).filter(l=>l.in_season), off=(S.sports||[]).filter(l=>!l.in_season);
  return `<h2>Sports</h2>${on.map(l=>`<div class="card" style="margin-bottom:12px">
    <div class="row"><div class="grow"><span class="sym">${esc(l.name)}</span></div>
     <label class="switch"><input type="checkbox" data-lg="${l.key}" ${l.on?'checked':''}><span></span></label></div>
    ${l.on?`<div class="row"><div class="grow">Top teams<div class="name" style="white-space:normal">${l.rankings?'From the AP poll':'Best records'}</div></div>
     <select data-lgtop="${l.key}">${l.top_choices.map(n=>`<option value="${n}" ${l.top==n?'selected':''}>${n?(l.rankings?'AP Top '+n:'Top '+n):'Off'}</option>`).join('')}</select></div>
    <div class="row" style="flex-wrap:wrap;gap:6px"><div class="grow" style="flex-basis:100%">Favorites<div class="name">Shown with a ★, plus their next game.</div></div>
     ${l.favorites.map(f=>`<span class="chip">★ ${esc(f.abbr)}<button data-unfav="${l.key}/${f.id}" aria-label="Remove">✕</button></span>`).join('')||'<span class="name">None yet</span>'}</div>
    <div class="row" style="border-top:0;padding-top:0"><input type="search" data-teamq="${l.key}" placeholder="Add a team, e.g. ${l.rankings?'alabama':'bills'}" autocomplete="off" autocorrect="off" autocapitalize="off"></div>
    <div id="teams-${l.key}"></div>`:''}
   </div>`).join('')}
   <p class="hint">A game shows if either team is a top team or a favorite: live games first, then finals from the last 24 hours, then each favorite's next game.${off.length?' Out of season (hidden): '+off.map(l=>esc(l.name)).join(', ')+'.':''}</p>`;
}
async function teamSearch(key,q,box){
  try{
    const {teams}=await api('/api/teams?league='+key+'&q='+encodeURIComponent(q));
    if(!teams.length){box.innerHTML='<div class="row"><div class="grow name">No matches</div></div>';return}
    box.innerHTML=teams.map((t,i)=>`<div class="row" data-ti="${i}"><div class="ph" style="background:#${/^[0-9a-f]{6}$/i.test(t.color||'')?t.color:'444'}"></div>
      <div class="grow"><div class="sym">${esc(t.abbr)}</div><div class="name">${esc(t.name)}</div></div><div class="add">＋</div></div>`).join('');
    box.onclick=e=>{const r=e.target.closest('[data-ti]');if(!r)return;const t=teams[+r.dataset.ti],l=lg(key);
      if(l.favorites.some(f=>f.id==t.id))return toast('Already a favorite');
      l.favorites.push({id:t.id,abbr:t.abbr});toast('Added '+t.abbr);save(0);render()};
  }catch(e){if(e.message!='login')box.innerHTML=`<div class="row"><div class="grow err">${esc(e.message)}</div></div>`}
}
function hourSel(id,val){return `<select id="${id}">${[...Array(24).keys()].map(h=>`<option value="${h}" ${h==val?'selected':''}>${(h%12||12)+(h<12?' am':' pm')}</option>`).join('')}</select>`}
function render(){
  const dim=S.dim_hours?S.dim_hours.split('-').map(Number):null;
  app.innerHTML=`<h1>Ticker</h1>
  <h2>Coins</h2><div class="card" id="coins">${S.coins.map((c,i)=>coinRow(c,i,S.coins.length)).join('')}</div>
  <p class="hint">★ = featured: gets a big-picture screen before its price. Tap a coin's picture to use your own.</p>
  <div class="card"><div class="row"><div class="grow">Focus<div class="name" style="white-space:normal">Show only ★ coins. Clock, Fear &amp; Greed and indices keep running.</div></div>
   <label class="switch"><input type="checkbox" id="focus" ${S.focus=='on'?'checked':''}><span></span></label></div></div>
  <p class="hint err" id="focushint" hidden>Focus is on but no coins are starred, so all coins are shown. Tap ★ on a coin.</p>
  <input type="file" id="file" accept="image/*" hidden>
  <h2>Add a coin</h2><input id="q" type="search" placeholder="Search name or ticker, e.g. cardano" autocomplete="off" autocorrect="off" autocapitalize="off">
  <div class="card" id="results" style="margin-top:8px"></div>
  <h2>Screens</h2><div class="card">
   ${screenOrder().map((k,i,a)=>`<div class="row"><div class="grow">${SCREEN_LABELS[k]}</div>
     <button class="btn" data-sup="${k}" ${i==0?'disabled':''} aria-label="Move up">↑</button>
     <button class="btn" data-sdown="${k}" ${i==a.length-1?'disabled':''} aria-label="Move down">↓</button>
     <label class="switch"><input type="checkbox" data-screen="${k}" ${S.screens.includes(k)?'checked':''}><span></span></label></div>`).join('')}
   ${S.screens.includes('indices')?`<div class="row"><div class="grow">Indices from<div class="name" style="white-space:normal">${S.indices_source=='yahoo'?'Real index values incl. the Dow. Unofficial source; may stop working.':'Tokenized S&amp;P 500 and Nasdaq funds. No extra CoinGecko calls.'}</div></div>
     <select id="isrc"><option value="coingecko" ${S.indices_source!='yahoo'?'selected':''}>CoinGecko</option><option value="yahoo" ${S.indices_source=='yahoo'?'selected':''}>Yahoo</option></select></div>
   <div class="row"><div class="grow">Also show $ / point change<div class="name" style="white-space:normal">${S.indices_source=='yahoo'
     ?'Flips from % to the change in index points halfway through.'
     :'Flips from % to the $ change halfway through. S&amp;P and Nasdaq are tokenized-fund prices ($ per share, not index points); switch the source to Yahoo for real points. The Dow is always points.'}</div></div>
     <label class="switch"><input type="checkbox" id="iview" ${S.indices_view=='both'?'checked':''}><span></span></label></div>`:''}
  </div>
  ${S.screens.includes('news')?newsRows():''}
  ${S.screens.includes('videos')?videosRows():''}
  <p class="hint">Shown in this order at the start of each cycle, then your coins. The weather screen uses the location below.</p>
  ${S.screens.includes('sports')?sportsSection():''}
  <h2>Weather on the clock</h2><div class="card">
   <div class="row"><div class="grow">Location<div class="name" id="wplace">${esc(S.weather_place||(S.weather_lat+', '+S.weather_lon))}</div></div></div>
   <div class="row" style="border-top:0;padding-top:0;gap:8px">
     <input id="zip" type="search" inputmode="text" placeholder="ZIP code or city" autocomplete="postal-code" style="flex:1">
     <button class="btn" id="zipgo" style="width:auto;padding:0 14px">Find</button></div>
   <div id="places"></div>
   <div class="row"><div class="grow">Temperature</div><div style="width:120px">${seg('temp_unit',[['F','°F'],['C','°C']])}</div></div>
  </div>
  <h2>Display</h2><div class="card">
   <div class="row"><div class="grow">Layout</div></div>
   <div class="row" style="border-top:0;padding-top:0">${seg('layout',[['classic','Icons'],['chart','Chart'],['mix','Mix']])}</div>
   <div class="row"><div class="grow">Brightness</div><div class="val" id="bv">${S.brightness}%</div></div>
   <div class="row" style="border-top:0;padding-top:0"><input type="range" id="bright" min="5" max="100" value="${S.brightness}"></div>
   <div class="row"><div class="grow">Seconds per coin</div><div class="val" id="sv">${S.sleep}s</div></div>
   <div class="row" style="border-top:0;padding-top:0"><input type="range" id="sleep" min="2" max="30" value="${S.sleep}"></div>
   <div class="row"><div class="grow">List view<div class="name" style="white-space:normal">Several coins per screen. Starred coins still get their big screens.</div></div>
    <label class="switch"><input type="checkbox" id="listview" ${S.list_view!='off'?'checked':''}><span></span></label></div>
   ${S.list_view!='off'?`<div class="row" style="border-top:0;padding-top:0">${seg('list_view',[['4','4 coins + %'],['5','5 coins']])}</div>
   <div class="row"><div class="grow">Seconds per page</div><div class="val" id="lpv">${S.list_page_seconds}s</div></div>
   <div class="row" style="border-top:0;padding-top:0"><input type="range" id="lpage" min="2" max="30" value="${S.list_page_seconds}"></div>`:''}
   <div class="row"><div class="grow">Transition<div class="name" style="white-space:normal">How one screen changes to the next.</div></div></div>
   <div class="row" style="border-top:0;padding-top:0">${seg('transition',[['scroll','Scroll'],['slide','Slide'],['none','None']])}</div>
  </div>
  <h2>Night dimming</h2><div class="card">
   <div class="row"><div class="grow">Dim at night</div><label class="switch"><input type="checkbox" id="dim" ${dim?'checked':''}><span></span></label></div>
   ${dim?`<div class="row"><div class="grow">From</div>${hourSel('dfrom',dim[0])}<div>to</div>${hourSel('dto',dim[1])}</div>
   <div class="row"><div class="grow">Night brightness</div><div class="val" id="dbv">${S.dim_brightness}%</div></div>
   <div class="row" style="border-top:0;padding-top:0"><input type="range" id="dbright" min="1" max="60" value="${S.dim_brightness}"></div>`:''}
  </div>
  <h2>Prices</h2><div class="card">
   <div class="row"><div class="grow">Currency</div><select id="cur">${['usd','eur','gbp','cad','aud','jpy'].map(c=>`<option ${S.currency==c?'selected':''}>${c}</option>`).join('')}</select></div>
   <div class="row"><div class="grow">Refresh prices every</div><select id="rr">${[['auto','Auto'],['60','1 min'],['120','2 min'],['180','3 min'],['300','5 min'],['600','10 min'],['900','15 min'],['1800','30 min']].map(([v,l])=>`<option value="${v}" ${String(S.refresh_rate)==v?'selected':''}>${l}</option>`).join('')}</select></div>
   ${usageRow()}
  </div>
  <p class="hint">Changes show on the ticker within a few seconds.${S.pin_set?'':' <span class="err">No PIN set: anyone on your Wi-Fi can change this. Add WEB_PIN to settings.env.</span>'}</p>
  <h2>Status</h2><div class="card" id="status"><div class="row"><div class="grow name">Loading…</div></div></div>
  <h2>Software</h2><div class="card" id="software"></div><div id="swhint"></div>`;
  bind();
  loadStatus();
}
// ----- status + software update -----
let statusTimer=null;
const dur=s=>{s=Math.max(0,Math.round(s));const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return d?`${d} d ${h} h`:h?`${h} h ${m} min`:m?`${m} min`:`${s} s`};
const ago=(t,now)=>t?(now-t<45?'just now':dur(now-t)+' ago'):'never';
function statusRows(st){
  const t=st.ticker,now=st.now,rows=[];
  const row=(l,v,cls='')=>`<div class="row"><div class="grow">${l}</div><div class="val ${cls}" style="min-width:0;text-align:right">${v}</div></div>`;
  if(t){
    rows.push(row('Last price update', t.last_success?esc(ago(t.last_success,now)):'not yet', t.stale||!t.last_success&&t.last_error?'err':''));
    if(t.stale)rows.push(`<div class="row" style="border-top:0;padding-top:0"><div class="grow name err" style="white-space:normal">Showing the last good prices (red dot on the panel).</div></div>`);
    rows.push(row('Ticker running for',esc(dur(now-t.started_at))));
  }
  if(st.system_uptime!=null)rows.push(row('Pi up for',esc(dur(st.system_uptime))));
  const w=st.wifi;
  rows.push(row('Wi-Fi signal',w?esc(`${w.label[0].toUpperCase()+w.label.slice(1)}${w.dbm!=null?' · '+w.dbm+' dBm':' · '+w.quality+'%'}`):'not available',w&&w.label=='weak'?'err':''));
  if(t){
    rows.push(row('Last error',t.last_error?esc(ago(t.last_error_at,now)):'none',t.last_error&&t.stale?'err':''));
    if(t.last_error)rows.push(`<div class="row" style="border-top:0;padding-top:0"><div class="grow name" style="white-space:normal;word-break:break-word">${esc(t.last_error.slice(0,200))}</div></div>`);
  }
  return rows.join('');
}
function softwareRows(u){
  if(!u)return '<div class="row"><div class="grow name">Updates aren\'t available here.</div></div>';
  const v=u.version, busy=u.state=='running'||u.state=='pending';
  const btn=(id,label,cls='')=>`<button class="btn ${cls}" id="${id}" style="width:auto;padding:0 14px" ${busy?'disabled':''}>${label}</button>`;
  const msgCls=['failed','rollback_failed'].includes(u.state)||u.needs_key?'err':'';
  return `<div class="row"><div class="grow">Version<div class="name">${esc(v.commit)} · ${esc(v.date)} · ${esc(v.subject)}</div></div></div>
   <div class="row" style="gap:8px"><div class="grow name ${msgCls}" style="white-space:normal">${esc(busy&&!u.message?'Working…':u.message||'')}</div>
   ${u.can_rollback?btn('rollback','Roll back','del'):''}${btn('update',busy?'Updating…':'Update')}</div>`;
}
function swHint(u){
  if(!u||!u.needs_key)return '';
  return u.deploy_key?`<p class="hint">Add this public key to the GitHub repo under <b>Settings → Deploy keys</b> (read-only), then tap Update again:</p>
   <div class="card"><div class="row"><div class="grow name" style="white-space:normal;word-break:break-all;font-family:ui-monospace,monospace;user-select:all">${esc(u.deploy_key)}</div></div></div>`
   :`<p class="hint">No deploy key on the Pi yet. Over SSH run <b>./install.sh --deploy-key</b>; it prints the key to add on GitHub (see the README).</p>`;
}
async function loadStatus(){
  clearTimeout(statusTimer);
  let st;
  try{st=await api('/api/status')}catch(e){statusTimer=setTimeout(loadStatus,5000);return}  // restarting
  if(!$('#status'))return;
  $('#status').innerHTML=statusRows(st);
  $('#software').innerHTML=softwareRows(st.update);
  $('#swhint').innerHTML=swHint(st.update);
  if($('#update'))$('#update').onclick=async()=>{
    if(!confirm('Update the ticker from GitHub? It restarts, and goes back to this version by itself if the update fails to start.'))return;
    try{await api('/api/update',{});toast('Updating…')}catch(e){if(e.message!='login')toast(e.message,true)}
    loadStatus()};
  if($('#rollback'))$('#rollback').onclick=async()=>{
    if(!confirm('Go back to the previous version ('+st.update.previous+')? The ticker restarts.'))return;
    try{await api('/api/rollback',{});toast('Rolling back…')}catch(e){if(e.message!='login')toast(e.message,true)}
    loadStatus()};
  const busy=st.update&&['running','pending'].includes(st.update.state);
  statusTimer=setTimeout(loadStatus,busy?3000:30000);
}
function save(extra){
  clearTimeout(saveTimer);
  saveTimer=setTimeout(async()=>{
    const body={symbols:S.coins.map(c=>c.entry).join(','),featured:(S.featured||[]).join(','),layout:S.layout,
      screens:S.screens.join(','),indices_source:S.indices_source,indices_view:S.indices_view,temp_unit:S.temp_unit,
      weather_lat:S.weather_lat,weather_lon:S.weather_lon,weather_place:S.weather_place,timezone:S.timezone,transition:S.transition,brightness:S.brightness,
      sleep:S.sleep,focus:S.focus,news_feeds:(S.news_feeds||[]).join('\n'),news_per_cycle:S.news_per_cycle,news_speed:S.news_speed,
      videos_channels:(S.videos_channels||[]).join('\n'),videos_per_cycle:S.videos_per_cycle,videos_max_age_days:S.videos_max_age_days,videos_skip_shorts:S.videos_skip_shorts,
      ...Object.fromEntries((S.sports||[]).flatMap(l=>[['sports_'+l.key,l.on?'on':'off'],['sports_'+l.key+'_top',String(l.top)],
        ['sports_'+l.key+'_favorites',l.favorites.map(f=>f.id+':'+f.abbr).join(',')]])),list_view:S.list_view,list_page_seconds:S.list_page_seconds,dim_hours:S.dim_hours,dim_brightness:S.dim_brightness,currency:S.currency,refresh_rate:S.refresh_rate,coins_meta:pendingMeta};
    try{await api('/api/settings',body);pendingMeta=[];toast('Saved ✓')}
    catch(e){if(e.message!='login'){toast('Not saved: '+e.message,true);setTimeout(load,1800)}}
  },extra===0?0:450);
}
function bind(){
  $('#coins').onclick=e=>{
    const r=e.target.closest('[data-reset]');
    if(r){e.preventDefault();resetIcon(S.coins[+r.dataset.reset]);return}
    const b=e.target.closest('button');if(!b)return;
    const L=S.coins;
    if(b.dataset.pic!==undefined){pickIcon(L[+b.dataset.pic]);return}
    if(b.dataset.star!==undefined){const sym=L[+b.dataset.star].entry.split(':')[0];S.featured=S.featured||[];
      S.featured=S.featured.includes(sym)?S.featured.filter(x=>x!=sym):[...S.featured,sym];
      b.classList.toggle('on');focusHint();save(0);return}
    if(b.dataset.del!==undefined){if(L.length<2)return toast('Keep at least one coin',true);L.splice(+b.dataset.del,1)}
    else if(b.dataset.up!==undefined){const i=+b.dataset.up;[L[i-1],L[i]]=[L[i],L[i-1]]}
    else if(b.dataset.down!==undefined){const i=+b.dataset.down;[L[i+1],L[i]]=[L[i],L[i+1]]}
    $('#coins').innerHTML=L.map((c,i)=>coinRow(c,i,L.length)).join('');save(0);
  };
  $('#q').oninput=e=>{clearTimeout(searchTimer);const q=e.target.value.trim();
    if(q.length<2){$('#results').innerHTML='';return}
    searchTimer=setTimeout(()=>search(q),350)};
  document.querySelectorAll('[data-seg]').forEach(s=>s.onclick=e=>{const b=e.target.closest('button');if(!b)return;
    S[s.dataset.seg]=b.dataset.v;s.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x==b));save();
    if(s.dataset.seg=='list_view')render()});
  document.querySelectorAll('[data-screen]').forEach(cb=>cb.onchange=()=>{
    const on=new Set(S.screens);cb.checked?on.add(cb.dataset.screen):on.delete(cb.dataset.screen);
    saveScreens(screenOrder(),on)});
  document.querySelectorAll('[data-sup],[data-sdown]').forEach(b=>b.onclick=()=>{
    const o=screenOrder(),k=b.dataset.sup||b.dataset.sdown,i=o.indexOf(k),j=b.dataset.sup?i-1:i+1;
    [o[i],o[j]]=[o[j],o[i]];S.screen_order=o;saveScreens(o,new Set(S.screens))});
  if($('#feeds'))$('#feeds').onchange=e=>{S.news_feeds=e.target.value.split(/\s+/).filter(Boolean);save()};
  if($('#nper'))$('#nper').onchange=e=>{S.news_per_cycle=+e.target.value;save()};
  if($('#ytch'))$('#ytch').onchange=e=>{S.videos_channels=e.target.value.split(/\s+/).filter(Boolean);save(0);setTimeout(load,1500);setTimeout(load,7000)};
  if($('#vper'))$('#vper').onchange=e=>{S.videos_per_cycle=+e.target.value;save()};
  if($('#vage'))$('#vage').onchange=e=>{S.videos_max_age_days=+e.target.value;save()};
  if($('#vshorts'))$('#vshorts').onchange=e=>{S.videos_skip_shorts=e.target.checked?'on':'off';save()};
  document.querySelectorAll('[data-lg]').forEach(cb=>cb.onchange=()=>{lg(cb.dataset.lg).on=cb.checked;save();render()});
  document.querySelectorAll('[data-lgtop]').forEach(sel=>sel.onchange=()=>{lg(sel.dataset.lgtop).top=+sel.value;save()});
  document.querySelectorAll('[data-unfav]').forEach(b=>b.onclick=()=>{const [k,id]=b.dataset.unfav.split('/');
    const l=lg(k);l.favorites=l.favorites.filter(f=>f.id!=id);save();render()});
  document.querySelectorAll('[data-teamq]').forEach(inp=>inp.oninput=()=>{clearTimeout(inp._t);
    const q=inp.value.trim(),box=$('#teams-'+inp.dataset.teamq);if(q.length<2){box.innerHTML='';return}
    inp._t=setTimeout(()=>teamSearch(inp.dataset.teamq,q,box),350)});
  if($('#isrc'))$('#isrc').onchange=e=>{S.indices_source=e.target.value;save();render()};
  if($('#iview'))$('#iview').onchange=e=>{S.indices_view=e.target.checked?'both':'percent';save()};
  const findPlace=async()=>{
    const q=$('#zip').value.trim();if(q.length<3)return toast('Enter a ZIP code or city',true);
    const box=$('#places');box.innerHTML='<div class="row"><div class="grow name">Looking up…</div></div>';
    try{
      const {places}=await api('/api/geocode?q='+encodeURIComponent(q));
      if(!places.length){box.innerHTML='<div class="row"><div class="grow name">No match. Try a city name.</div></div>';return}
      box.innerHTML=places.map((p,i)=>`<div class="row" data-p="${i}"><div class="grow">${esc(p.label)}<div class="name">${p.timezone?esc(p.timezone):''}</div></div><div class="add">＋</div></div>`).join('');
      box.onclick=e=>{const r=e.target.closest('[data-p]');if(!r)return;const p=places[+r.dataset.p];
        S.weather_lat=p.lat;S.weather_lon=p.lon;S.weather_place=p.label;if(p.timezone)S.timezone=p.timezone;
        $('#wplace').textContent=p.label;box.innerHTML='';$('#zip').value='';toast('Weather: '+p.label);save(0)};
    }catch(e){if(e.message!='login')box.innerHTML=`<div class="row"><div class="grow err">${esc(e.message)}</div></div>`}
  };
  $('#zipgo').onclick=findPlace;$('#zip').onkeydown=e=>{if(e.key=='Enter')findPlace()};
  $('#bright').oninput=e=>{S.brightness=+e.target.value;$('#bv').textContent=S.brightness+'%';save()};
  $('#sleep').oninput=e=>{S.sleep=+e.target.value;$('#sv').textContent=S.sleep+'s';save()};
  $('#listview').onchange=e=>{S.list_view=e.target.checked?'4':'off';save();render()};
  $('#focus').onchange=e=>{S.focus=e.target.checked?'on':'off';focusHint();save()};
  if($('#lpage'))$('#lpage').oninput=e=>{S.list_page_seconds=+e.target.value;$('#lpv').textContent=S.list_page_seconds+'s';save()};
  focusHint();
  $('#dim').onchange=e=>{S.dim_hours=e.target.checked?'22-7':'';render();save()};
  if($('#dfrom')){const f=()=>{S.dim_hours=$('#dfrom').value+'-'+$('#dto').value;save()};$('#dfrom').onchange=f;$('#dto').onchange=f;
    $('#dbright').oninput=e=>{S.dim_brightness=+e.target.value;$('#dbv').textContent=S.dim_brightness+'%';save()}}
  $('#cur').onchange=e=>{S.currency=e.target.value;save()};
  $('#rr').onchange=e=>{S.refresh_rate=e.target.value;save();setTimeout(load,1200)};
}
function usageRow(){
  const u=S.usage;if(!u)return '';
  const pct=Math.min(100,Math.round(u.calls/u.budget*100));
  const every=u.interval<90?'every minute':`every ${(u.interval/60).toFixed(u.interval%60?1:0)} min`;
  const perMonth=Math.round(30*24*3600/u.interval);
  const note=S.refresh_rate=='auto'
    ? `Auto: refreshing ${every}. Speeds up or slows down to stay under the free limit.`
    : (perMonth>u.budget?`<span class="err">If left on 24/7 this rate would use ~${perMonth.toLocaleString()} calls a month.</span> Fine if the ticker is off part of the day; Auto handles this for you.`:`Refreshing ${every}.`);
  return `<div class="row" style="display:block"><div style="display:flex;justify-content:space-between"><span>Calls this month</span>
    <span class="val">${u.calls.toLocaleString()} / ${u.budget.toLocaleString()}</span></div>
    <div style="height:6px;background:#0f1216;border-radius:3px;margin:8px 0 6px"><div style="height:6px;width:${pct}%;border-radius:3px;background:${pct>85?'var(--down)':'var(--up)'}"></div></div>
    <div class="name" style="white-space:normal">${note}</div></div>`;
}
function pickIcon(c){
  const f=$('#file');f.value='';
  f.onchange=async()=>{
    const file=f.files[0];if(!file)return;
    try{
      // shrink on the phone first: small upload, and HEIC photos become PNG
      const bmp=await createImageBitmap(file);const k=Math.min(1,256/Math.max(bmp.width,bmp.height));
      const cv=document.createElement('canvas');cv.width=Math.round(bmp.width*k);cv.height=Math.round(bmp.height*k);
      cv.getContext('2d').drawImage(bmp,0,0,cv.width,cv.height);
      const blob=await new Promise(r=>cv.toBlob(r,'image/png'));
      const sym=c.entry.split(':')[0];
      const r=await fetch('/api/icon?sym='+encodeURIComponent(sym),{method:'POST',headers:{'Content-Type':'image/png'},body:blob});
      if(r.status==401)return showLogin();
      if(!r.ok)throw new Error((await r.json()).error);
      toast('New picture for '+c.symbol);load();
    }catch(e){toast(e.message||'Upload failed',true)}
  };
  f.click();
}
async function resetIcon(c){
  const sym=c.entry.split(':')[0];
  try{await api('/api/icon/reset?sym='+encodeURIComponent(sym),{});toast(c.symbol+' back to default');load()}
  catch(e){if(e.message!='login')toast(e.message,true)}
}
async function search(q){
  const box=$('#results');box.innerHTML='<div class="row"><div class="grow name">Searching…</div></div>';
  try{
    const {coins}=await api('/api/search?q='+encodeURIComponent(q));
    if(!coins.length){box.innerHTML='<div class="row"><div class="grow name">No matches</div></div>';return}
    box.innerHTML=coins.map((c,i)=>`<div class="row" data-i="${i}">${c.thumb?`<img src="${esc(c.thumb)}" alt="">`:'<div class="ph"></div>'}
      <div class="grow"><div class="sym">${esc(c.symbol)}</div><div class="name">${esc(c.name)}</div></div>
      <div class="rank">${c.market_cap_rank?'#'+c.market_cap_rank:''}</div><div class="add">＋</div></div>`).join('');
    box.onclick=e=>{const r=e.target.closest('[data-i]');if(!r)return;const c=coins[+r.dataset.i];
      const sym=c.symbol.toLowerCase().replace(/[^a-z0-9.\-]/g,'').slice(0,20)||'coin';
      const entry=sym+':'+c.id;
      if(S.coins.some(x=>x.entry==entry||x.entry.split(':')[1]==c.id))return toast('Already on the list');
      S.coins.push({entry,symbol:sym.toUpperCase(),name:c.name,image:c.thumb});pendingMeta.push(c);
      $('#coins').innerHTML=S.coins.map((x,i)=>coinRow(x,i,S.coins.length)).join('');
      box.innerHTML='';$('#q').value='';toast('Added '+c.symbol);save(0)};
  }catch(e){if(e.message!='login')box.innerHTML=`<div class="row"><div class="grow err">${esc(e.message)}</div></div>`}
}
api('/api/state').then(s=>s.authed?load():showLogin());
</script></body></html>
"""
