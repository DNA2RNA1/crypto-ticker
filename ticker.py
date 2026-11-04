#!/usr/bin/env python3
"""Crypto price ticker for an RGB LED matrix on a Raspberry Pi.

Run on the Pi:        sudo ./venv/bin/python ticker.py
Try it on a PC:       python ticker.py --emulate      (needs RGBMatrixEmulator)
  offline, made-up:   python ticker.py --emulate --sample
Settings come from environment variables or settings.env (see settings.env.example).
"""

import argparse
import logging
from collections import deque
import math
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from PIL import Image

from zoneinfo import ZoneInfo

from feeds import FearGreed, Indices, Metals, Weather, parse_indices
from session import session_change
from cache import RequestCache
from icons import IconStore
from logsetup import setup_logging
from prices import CoinGecko, DataError, NetworkError, PriceError, RateLimitError
from portfolio import parse_holdings, summarize
from render import Renderer
from retry import backoff_delay
from news import DEFAULT_FEEDS, DEFAULT_GENERAL_FEEDS, DEFAULT_POLITICS_FEEDS, News
from videos import TAG_COLOR as VIDEO_TAG, Videos, parse_entry as parse_channel
from arcade import Animation, Arcade
from markets import Markets, entries_from_env as markets_from_env
from watchlist import REFRESH_CHOICES as WATCHLIST_REFRESH, Watchlist, entries_from_env as watchlist_from_env
from scroller import SPEEDS, Scroller
from sports import BY_KEY as LEAGUE_BY_KEY, LIVE_EVERY, IDLE_EVERY, Sports, follow_from_env
from usage import Usage

HERE = os.path.dirname(os.path.abspath(__file__))
REQUEST_CACHE = os.path.join(HERE, "cache", "requests.json")

SCREEN_NAMES = ("clock", "weather", "feargreed", "indices", "metals", "watchlist", "news", "videos", "sports",
                "odds", "arcade", "coins")
DEFAULT_SCREENS = ("clock", "feargreed", "indices", "coins")  # new screens are opt-in
TRANSITIONS = ("scroll", "slide", "none")
# Tokenized index funds listed on CoinGecko: fetched in the same call as the coins
CG_INDICES = [("spx", "sp500-xstock", "S&P"), ("ndq", "nasdaq-xstock", "NAS")]
log = logging.getLogger("ticker")


def load_env_file(path, override=False):
    """Read KEY=value lines into os.environ (existing variables win unless override)."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if override:
                os.environ[key.strip()] = value
            else:
                os.environ.setdefault(key.strip(), value)


def env(name, default, cast=str, lo=None, hi=None, choices=None):
    """Read a setting. A bad value (typo in settings.env) logs a warning and falls back
    to the default instead of stopping the ticker; numbers are clamped to [lo, hi]."""
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return default
    if cast is bool:
        if raw.lower() in ("1", "true", "yes", "on"):
            return True
        if raw.lower() in ("0", "false", "no", "off"):
            return False
        log.warning("ignoring %s=%r (use true or false); using %r", name, raw, default)
        return default
    try:
        value = cast(raw)
    except ValueError:
        log.warning("ignoring %s=%r (not a valid %s); using %r", name, raw, cast.__name__, default)
        return default
    if isinstance(value, float) and not math.isfinite(value):
        log.warning("ignoring %s=%r; using %r", name, raw, default)
        return default
    if choices is not None and value not in choices:
        log.warning("ignoring %s=%r (choose one of %s); using %r", name, raw, ", ".join(choices), default)
        return default
    if lo is not None and value < lo:
        log.warning("%s=%r is below %s; using %s", name, raw, lo, lo)
        value = lo
    if hi is not None and value > hi:
        log.warning("%s=%r is above %s; using %s", name, raw, hi, hi)
        value = hi
    return value


def parse_weather_extra(spec):
    """'Santa Fe, New Mexico, US|35.687|-105.938;...' -> [(place, lat, lon)], up to 4."""
    out = []
    for part in (spec or "").split(";"):
        bits = part.split("|")
        if len(bits) != 3:
            continue
        try:
            lat, lon = float(bits[1]), float(bits[2])
        except ValueError:
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180 and bits[0].strip():
            out.append((bits[0].strip(), lat, lon))
    return out[:4]


def news_urls(cfg):
    """The feeds the news screen reads: crypto, general and politics, each switchable."""
    urls = (list(cfg.news_feeds) if cfg.news_crypto else []) + \
        (list(cfg.news_general_feeds) if cfg.news_general else []) + \
        (list(cfg.news_politics_feeds) if cfg.news_politics else [])
    return list(dict.fromkeys(urls))


@dataclass
class Config:
    symbols: list = field(default_factory=lambda: ["btc", "eth"])
    currency: str = "usd"
    api_key: str = ""
    api_pro: bool = False
    refresh_rate: int = 0          # seconds; 0 = auto (fit the monthly budget)
    monthly_budget: int = 10000
    sleep: float = 5
    transition: str = "scroll"     # scroll (up), slide (left) or none
    layout: str = "classic"
    featured: list = field(default_factory=list)
    focus: bool = False            # only starred (FEATURED) coins in the coin rotation
    portfolio: bool = False        # portfolio screen (reset to off at every start)
    portfolio_private: bool = True # no dollar amounts (reset to on at every start)
    portfolio_pct: str = "24h"     # private mode shows the 24h %, or "total" (since cost)
    holdings: dict = field(default_factory=dict)  # symbol -> (quantity, cost basis or None)
    list_view: int = 0             # 0 = one coin per screen; 5 or 4 = coins per list page
    list_page_seconds: float = 6
    screens: list = field(default_factory=lambda: list(DEFAULT_SCREENS))
    indices_source: str = "coingecko"
    indices: str = ""
    indices_view: str = "percent"  # percent, or both (% then the $ / point change)
    indices_style: str = "charts"  # charts = one big chart per index; compact = all three on one
    news_feeds: list = field(default_factory=lambda: list(DEFAULT_FEEDS))
    news_per_cycle: int = 3
    news_crypto: bool = True       # the crypto feeds (NEWS_FEEDS)
    news_general: bool = False     # general news feeds (NEWS_GENERAL_FEEDS)
    news_general_feeds: list = field(default_factory=lambda: list(DEFAULT_GENERAL_FEEDS))
    news_politics: bool = False    # politics feeds (NEWS_POLITICS_FEEDS)
    news_politics_feeds: list = field(default_factory=lambda: list(DEFAULT_POLITICS_FEEDS))
    news_speed: str = "normal"     # slow, normal, fast (see scroller.SPEEDS)
    videos_channels: list = field(default_factory=list)  # "UC..." ids or "@handles"
    videos_per_cycle: int = 3
    arcade_per_cycle: int = 1      # arcade animation scenes per cycle (they rotate)
    watchlist: list = field(default_factory=list)  # [(Yahoo symbol, panel label)]
    watchlist_refresh: int = 10    # minutes between watchlist refreshes
    watchlist_per_cycle: int = 5   # watchlist symbols per cycle (they rotate)
    sports_odds: bool = False      # show the betting line after upcoming games (display only)
    odds_markets: list = field(default_factory=list)  # "poly:<slug>" / "kalshi:<TICKER>"
    odds_per_cycle: int = 3        # prediction markets per cycle (they rotate)
    videos_max_age_days: int = 3
    videos_skip_shorts: bool = True
    sports: dict = field(default_factory=lambda: follow_from_env({}))  # league key -> Follow
    timezone: str = ""
    weather_lat: float = 26.3017   # Edinburg, TX
    weather_lon: float = -98.1633
    temp_unit: str = "F"
    weather_place: str = "Edinburg, Texas, US"
    weather_extra: list = field(default_factory=list)  # [(place, lat, lon)] extra weather cities
    brightness: int = 70
    dim_hours: str = ""
    dim_brightness: int = 15
    download_icons: bool = True
    rows: int = 32
    cols: int = 64
    chain: int = 1
    parallel: int = 1
    gpio_mapping: str = "adafruit-hat"
    gpio_slowdown: int = 1
    pwm_bits: int = 11
    pwm_lsb_nanoseconds: int = 130
    rgb_sequence: str = "RGB"
    panel_type: str = ""
    no_hardware_pulse: bool = False

    @classmethod
    def from_env(cls):
        c = cls()
        c.symbols = [s for s in env("SYMBOLS", "btc,eth").split(",") if s.strip()]
        c.currency = env("CURRENCY", c.currency).lower()
        c.api_key = env("COINGECKO_API_KEY", "")
        c.api_pro = env("COINGECKO_PRO", False, bool)
        rr = env("REFRESH_RATE", "auto").strip().lower()
        if rr in ("", "auto", "0"):
            c.refresh_rate = 0
        else:
            c.refresh_rate = env("REFRESH_RATE", 0, int, lo=60, hi=86400)
        c.monthly_budget = env("MONTHLY_CALL_BUDGET", c.monthly_budget, int, lo=100)
        c.sleep = env("SLEEP", c.sleep, float, lo=1, hi=300)
        c.transition = env("TRANSITION", c.transition, str.lower, choices=TRANSITIONS)
        c.layout = env("LAYOUT", c.layout, str.lower, choices=Renderer.LAYOUTS)
        c.featured = [s.strip().lower() for s in env("FEATURED", "").split(",") if s.strip()]
        c.focus = env("FOCUS", False, bool)
        c.portfolio = env("PORTFOLIO", False, bool)
        c.portfolio_private = env("PORTFOLIO_PRIVATE", True, bool)
        c.portfolio_pct = env("PORTFOLIO_PCT", c.portfolio_pct, str.lower, choices=("24h", "total"))
        c.holdings = parse_holdings(os.environ.get("PORTFOLIO_HOLDINGS", ""))
        lv = env("LIST_VIEW", "off", str.lower, choices=("off", "5", "4"))
        c.list_view = 0 if lv == "off" else int(lv)
        c.list_page_seconds = env("LIST_PAGE_SECONDS", c.list_page_seconds, float, lo=2, hi=60)
        screens = [s.strip().lower() for s in env("SCREENS", ",".join(DEFAULT_SCREENS)).split(",")]
        c.screens = [s for s in screens if s in SCREEN_NAMES] or ["coins"]
        c.indices_source = env("INDICES_SOURCE", c.indices_source, str.lower, choices=("coingecko", "yahoo"))
        c.indices = env("INDICES", "")
        c.indices_view = env("INDICES_VIEW", c.indices_view, str.lower, choices=("percent", "both"))
        c.indices_style = env("INDICES_STYLE", c.indices_style, str.lower, choices=("charts", "compact"))
        feeds = [u.strip() for u in env("NEWS_FEEDS", ",".join(DEFAULT_FEEDS)).split(",") if u.strip()]
        c.news_feeds = [u for u in feeds if u.lower().startswith(("http://", "https://"))][:8] or list(DEFAULT_FEEDS)
        c.news_per_cycle = env("NEWS_PER_CYCLE", c.news_per_cycle, int, lo=1, hi=10)
        c.news_crypto = env("NEWS_CRYPTO", True, bool)
        c.news_general = env("NEWS_GENERAL", False, bool)
        general = [u.strip() for u in env("NEWS_GENERAL_FEEDS", ",".join(DEFAULT_GENERAL_FEEDS)).split(",")
                   if u.strip()]
        c.news_general_feeds = [u for u in general if u.lower().startswith(("http://", "https://"))][:8]
        c.news_politics = env("NEWS_POLITICS", False, bool)
        politics = [u.strip() for u in env("NEWS_POLITICS_FEEDS", ",".join(DEFAULT_POLITICS_FEEDS)).split(",")
                    if u.strip()]
        c.news_politics_feeds = [u for u in politics if u.lower().startswith(("http://", "https://"))][:8]
        c.news_speed = env("NEWS_SPEED", c.news_speed, str.lower, choices=tuple(SPEEDS))
        c.videos_channels = channels_from_env(env("VIDEOS_CHANNELS", ""))
        c.videos_per_cycle = env("VIDEOS_PER_CYCLE", c.videos_per_cycle, int, lo=1, hi=10)
        c.arcade_per_cycle = env("ARCADE_PER_CYCLE", c.arcade_per_cycle, int, lo=1, hi=6)
        c.watchlist = watchlist_from_env(env("WATCHLIST", ""))
        c.watchlist_refresh = int(env("WATCHLIST_REFRESH", str(c.watchlist_refresh),
                                      choices=tuple(str(m) for m in WATCHLIST_REFRESH)))
        c.watchlist_per_cycle = env("WATCHLIST_PER_CYCLE", c.watchlist_per_cycle, int, lo=1, hi=20)
        c.sports_odds = env("SPORTS_ODDS", False, bool)
        c.odds_markets = markets_from_env(env("ODDS_MARKETS", ""))
        c.odds_per_cycle = env("ODDS_PER_CYCLE", c.odds_per_cycle, int, lo=1, hi=10)
        c.videos_max_age_days = env("VIDEOS_MAX_AGE_DAYS", c.videos_max_age_days, int, lo=1, hi=30)
        c.videos_skip_shorts = env("VIDEOS_SKIP_SHORTS", c.videos_skip_shorts, bool)
        c.sports = follow_from_env(os.environ)
        c.timezone = env("TIMEZONE", "")
        c.weather_lat = env("WEATHER_LAT", c.weather_lat, float, lo=-90.0, hi=90.0)
        c.weather_lon = env("WEATHER_LON", c.weather_lon, float, lo=-180.0, hi=180.0)
        c.temp_unit = "C" if env("TEMP_UNIT", "F").upper().startswith("C") else "F"
        c.weather_place = env("WEATHER_PLACE", c.weather_place)
        c.weather_extra = parse_weather_extra(env("WEATHER_EXTRA", ""))
        c.brightness = env("BRIGHTNESS", c.brightness, int, lo=1, hi=100)
        c.dim_hours = env("DIM_HOURS", "")
        c.dim_brightness = env("DIM_BRIGHTNESS", c.dim_brightness, int, lo=1, hi=100)
        c.download_icons = env("DOWNLOAD_ICONS", True, bool)
        c.rows = env("LED_ROWS", c.rows, int, lo=8, hi=128)
        c.cols = env("LED_COLS", c.cols, int, lo=16, hi=256)
        c.chain = env("LED_CHAIN", c.chain, int, lo=1, hi=8)
        c.parallel = env("LED_PARALLEL", c.parallel, int, lo=1, hi=3)
        c.gpio_mapping = env("LED_GPIO_MAPPING", c.gpio_mapping)
        c.gpio_slowdown = env("LED_SLOWDOWN_GPIO", c.gpio_slowdown, int, lo=0, hi=5)
        c.pwm_bits = env("LED_PWM_BITS", c.pwm_bits, int, lo=1, hi=11)
        c.pwm_lsb_nanoseconds = env("LED_PWM_LSB_NANOSECONDS", c.pwm_lsb_nanoseconds, int, lo=50, hi=3000)
        c.rgb_sequence = env("LED_RGB_SEQUENCE", c.rgb_sequence)
        c.panel_type = env("LED_PANEL_TYPE", "")
        c.no_hardware_pulse = env("LED_NO_HARDWARE_PULSE", False, bool)
        return c


def channels_from_env(raw):
    """VIDEOS_CHANNELS -> canonical channel entries; a bad one is logged and skipped."""
    out = []
    for item in (x.strip() for x in raw.split(",")):
        if not item:
            continue
        try:
            entry = parse_channel(item)
        except ValueError as exc:
            log.warning("ignoring YouTube channel %r: %s", item, exc)
            continue
        if entry not in out:
            out.append(entry)
    return out[:10]


def get_tz(name):
    try:
        return ZoneInfo(name) if name else None
    except Exception:
        log.warning("unknown TIMEZONE %r, using the Pi's clock setting", name)
        return None


def transition_frames(old, new, vertical=False, frames=14):
    """The in-between frames of a scroll (old moves up, new rolls in from the bottom)
    or slide (old moves left, new comes in from the right). Ease-out: fast, then settling."""
    size = old.height if vertical else old.width
    out = []
    for i in range(1, frames):
        t = 1 - (1 - i / frames) ** 3  # ease-out cubic
        d = round(size * t)
        frame = Image.new("RGB", old.size)
        if vertical:
            frame.paste(old, (0, -d))
            frame.paste(new, (0, size - d))
        else:
            frame.paste(old, (-d, 0))
            frame.paste(new, (size - d, 0))
        out.append(frame)
    return out


def in_dim_window(spec, now=None):
    """spec like '22-7' (10pm to 7am). Empty spec means never dim."""
    if not spec or "-" not in spec:
        return False
    try:
        start, end = (int(p) % 24 for p in spec.split("-", 1))
    except ValueError:
        return False
    hour = (now or datetime.now()).hour
    return start <= hour < end if start < end else (hour >= start or hour < end)


HARDWARE_FIELDS = ("rows", "cols", "chain", "parallel", "gpio_mapping", "gpio_slowdown", "pwm_bits",
                   "pwm_lsb_nanoseconds", "rgb_sequence", "panel_type", "no_hardware_pulse")


class Ticker:
    def __init__(self, cfg, display, env_path=None):
        self.cfg = cfg
        self.display = display
        self.env_path = env_path
        self.wake = threading.Event()  # set by the settings page to cut a wait short
        self.reload_pending = False
        if hasattr(display, "wake"):
            display.wake = self.wake
        self.usage = Usage(cfg.monthly_budget)
        self.request_cache = RequestCache(REQUEST_CACHE)
        self.api = self._make_api(cfg)
        self.icons = IconStore(download=cfg.download_icons)
        self.renderer = Renderer(display.width, display.height, cfg.currency, self.icons,
                                 layout=cfg.layout)
        self.assets = []
        self.index_rows = []
        self.fear_greed = FearGreed()
        self.weather = Weather(cfg.weather_lat, cfg.weather_lon,
                               "celsius" if cfg.temp_unit == "C" else "fahrenheit")
        self.extra_weather = self._make_extra_weather(cfg)
        self.yahoo = Indices(parse_indices(cfg.indices))
        self.yahoo_dow = Indices([("^DJI", "DOW")])  # no Dow token exists on CoinGecko
        self.metals = Metals()
        self.news = News(news_urls(cfg))
        self.videos = self._make_videos(cfg)
        self.arcade = Arcade()
        self.watchlist = Watchlist(cfg.watchlist, cfg.watchlist_refresh)
        self.markets = Markets(cfg.odds_markets)
        self.sports = Sports(cfg.sports)
        self.tz = get_tz(cfg.timezone)
        self.stale = False          # True while showing the last good prices, not fresh ones
        self.stale_since = None     # when those last good prices were fetched
        self.failures = 0           # consecutive failed refreshes (drives the backoff)
        self.last_success = None
        self.last_error_at = None
        self.started_at = time.time()
        self.last_error = None
        self.next_fetch = 0.0
        self.current = None
        self._fetching = None       # background fetch thread, if one is running
        self.frame_times = deque(maxlen=2000)  # seconds to build + push each scroll frame

    @staticmethod
    def _make_extra_weather(cfg):
        unit = "celsius" if cfg.temp_unit == "C" else "fahrenheit"
        return [(place, Weather(lat, lon, unit)) for place, lat, lon in cfg.weather_extra]

    def _cg_indices(self, cfg):
        return "indices" in cfg.screens and cfg.indices_source == "coingecko"

    def _needs_prices(self, cfg):
        """CoinGecko is only called when something on screen uses it."""
        return "coins" in cfg.screens or self._cg_indices(cfg)

    @staticmethod
    def _make_videos(cfg):
        return Videos(cfg.videos_channels, cfg.videos_max_age_days, cfg.videos_skip_shorts)

    def _make_api(self, cfg):
        symbols = list(cfg.symbols)
        if self._cg_indices(cfg):  # rides along in the same request: no extra calls
            symbols += [f"{sym}:{cid}" for sym, cid, _ in CG_INDICES]
        return CoinGecko(symbols, cfg.currency, cfg.api_key, cfg.api_pro,
                         on_call=self.usage.count_call, cache=self.request_cache)

    def now(self):
        return datetime.now(self.tz)

    def interval(self):
        """Seconds between price refreshes (fixed, or auto from the budget)."""
        return self.cfg.refresh_rate or self.usage.auto_interval()

    def refresh(self, now=None, wait=False):
        """Start a price fetch in the background if one is due. The display keeps
        rotating while it runs, so a slow network never freezes the panel.
        wait=True blocks until it finishes (used by tests and preview.py)."""
        now = now or time.time()
        t = self._fetching
        if t is None and not self._needs_prices(self.cfg):
            return  # crypto prices are off and nothing else needs CoinGecko
        if t is None and now >= self.next_fetch:
            api = self.api
            self.next_fetch = now + self.interval()  # provisional; set again when done
            t = self._fetching = threading.Thread(target=self._fetch, args=(api,),
                                                  daemon=True, name="prices")
            t.start()
        if wait and t is not None:
            t.join()

    def _fetch(self, api):
        try:
            fetched = api.fetch()
        except PriceError as exc:
            self._fetch_failed(api, exc)
        except Exception as exc:  # a bug or surprise must not kill the ticker
            log.exception("unexpected error fetching prices")
            self._fetch_failed(api, PriceError(f"unexpected error: {exc}"))
        else:
            self._fetch_ok(api, fetched)
        finally:
            self._fetching = None

    def _split(self, fetched):
        """Separate tokenized index funds from the coins."""
        index_ids = {cid for _, cid, _ in CG_INDICES} if self._cg_indices(self.cfg) else set()
        labels = {cid: label for _, cid, label in CG_INDICES}
        assets = [a for a in fetched if a["id"] not in index_ids]
        index_rows = []
        for a in fetched:
            if a["id"] in index_ids:
                change, is_open, chart, dollars = session_change(
                    a["price"], a["sparkline"], a.get("updated"))
                index_rows.append({
                    "label": labels[a["id"]], "spark": chart or a["sparkline"], "open": is_open,
                    "abs": dollars, "unit": "$",  # per tokenized fund share, in USD
                    # fall back to the rolling 24h figure only if history is missing
                    "change": change if change is not None else (a["change_24h"] or 0.0)})
        return assets, index_rows

    def _fetch_ok(self, api, fetched):
        if api is not self.api:  # settings changed mid-fetch; the new list is fetched next
            return
        self.assets, self.index_rows = self._split(fetched)
        if self.stale:
            log.info("prices are live again after %d failed refresh(es)", self.failures)
        self.stale = False
        self.stale_since = None
        self.failures = 0
        self.last_error = None
        self.last_success = time.time()
        self.next_fetch = self.last_success + self.interval()
        log.info("prices: %s", ", ".join(f"{a['symbol']} {a['price']:g}" for a in self.assets))

    def retry_delay(self, exc):
        """Seconds to wait after the latest failure: exponential backoff, and at least
        the server's Retry-After for a 429."""
        if isinstance(exc, RateLimitError):
            delay = backoff_delay(self.failures, base=120, cap=1800)
            if exc.retry_after:
                delay = max(delay, min(exc.retry_after, 3600))
            return max(60.0, delay)
        # network trouble: 30s, 60s, 2 min, 4 min, ... up to 10 min (or the normal interval)
        return backoff_delay(self.failures, base=30, cap=max(600, self.interval()))

    def _fetch_failed(self, api, exc):
        if api is not self.api:
            return
        self.failures += 1
        self.last_error = exc
        self.last_error_at = time.time()
        delay = self.retry_delay(exc)
        self.next_fetch = time.time() + delay
        if not self.assets:  # cold start offline: fall back to the cached last good prices
            cached, fetched_at = api.last_good()
            if cached:
                self.assets, self.index_rows = self._split(cached)
                self.stale_since = fetched_at
                log.warning("showing cached prices from %s",
                            datetime.fromtimestamp(fetched_at).strftime("%Y-%m-%d %H:%M"))
        if self.assets:
            self.stale = True
            if self.stale_since is None:
                self.stale_since = self.last_success or time.time()
        log.error("price fetch failed (%d in a row): %s; next try in %ds%s", self.failures, exc,
                  delay, "; showing last good prices as stale" if self.assets else "")

    def status(self):
        """For the phone page's Status section."""
        return {
            "last_success": self.last_success,
            "stale": self.stale,
            "failures": self.failures,
            "last_error": str(self.last_error) if self.last_error else None,
            "last_error_at": self.last_error_at,
            "started_at": self.started_at,
            "coins": len(self.assets),
        }

    def error_reason(self):
        exc = self.last_error
        if isinstance(exc, RateLimitError):
            return "RATE LIMIT"
        if isinstance(exc, NetworkError):
            return "NO NETWORK?"
        if isinstance(exc, DataError):
            return "BAD DATA"
        return "API ERROR"

    def request_reload(self):
        """Called from the settings page after it saves settings.env."""
        self.reload_pending = True
        self.wake.set()

    def reload(self):
        self.reload_pending = False
        self.wake.clear()
        if not self.env_path:
            return
        load_env_file(self.env_path, override=True)
        new = Config.from_env()
        for name in HARDWARE_FIELDS:  # panel wiring can't change without a restart
            setattr(new, name, getattr(self.cfg, name))
        old = self.cfg
        if (new.symbols, new.currency, new.api_key, new.api_pro, self._cg_indices(new)) != \
                (old.symbols, old.currency, old.api_key, old.api_pro, self._cg_indices(old)):
            self.api = self._make_api(new)
            self.next_fetch = 0  # fetch the new list right away
        self.usage.budget = new.monthly_budget
        self.tz = get_tz(new.timezone)
        if (new.weather_lat, new.weather_lon, new.temp_unit) != \
                (old.weather_lat, old.weather_lon, old.temp_unit):
            self.weather = Weather(new.weather_lat, new.weather_lon,
                                   "celsius" if new.temp_unit == "C" else "fahrenheit")
        if (new.weather_extra, new.temp_unit) != (old.weather_extra, old.temp_unit):
            self.extra_weather = self._make_extra_weather(new)
        if new.sports != old.sports:
            self.sports.follow = new.sports
            self.sports.next_fetch = 0  # fetch with the new teams right away
        if news_urls(new) != news_urls(old):
            self.news = News(news_urls(new))
        if (new.videos_channels, new.videos_max_age_days, new.videos_skip_shorts) != \
                (old.videos_channels, old.videos_max_age_days, old.videos_skip_shorts):
            self.videos = self._make_videos(new)
            if "videos" in new.screens:
                self.videos.update()  # check the channels now, so the phone page can show it
        if new.indices != old.indices:
            self.yahoo = Indices(parse_indices(new.indices))
        if new.odds_markets != old.odds_markets:
            self.markets = Markets(new.odds_markets)
            if "odds" in new.screens:
                self.markets.update()  # check the links now, so the phone page can show it
        if (new.watchlist, new.watchlist_refresh) != (old.watchlist, old.watchlist_refresh):
            self.watchlist = Watchlist(new.watchlist, new.watchlist_refresh)
            if "watchlist" in new.screens:
                self.watchlist.update()  # check the symbols now, so the phone page can show it
        self.cfg = new
        if not self.failures:  # never cut short a backoff (e.g. after HTTP 429)
            self.next_fetch = min(self.next_fetch, time.time() + self.interval())
        self.renderer.layout = new.layout if new.layout in Renderer.LAYOUTS else "classic"
        self.renderer.currency = new.currency
        self.icons.forget()  # pick up any newly uploaded icons
        self.cfg = new
        self.apply_brightness()
        log.info("settings reloaded: %s", ",".join(new.symbols))

    def search(self, query):
        return self.api.search(query)

    def apply_brightness(self):
        dim = in_dim_window(self.cfg.dim_hours, self.now())
        self.display.set_brightness(self.cfg.dim_brightness if dim else self.cfg.brightness)

    def show(self, image, hold, slide=True):
        """Show a screen, animating from the previous one unless ``slide`` is False
        (used when a screen changes in place, like the indices % / $ flip)."""
        if slide and self.current is not None and self.cfg.transition in ("scroll", "slide"):
            self.transition(self.current, image, vertical=self.cfg.transition == "scroll")
        self.display.show(image, hold)
        self.current = image

    def play(self, anim):
        """Show an animated (scrolling) screen. It comes in with the normal transition,
        scrolls at a steady speed, and its last frame is where the next transition starts.

        On a real panel the text position follows the clock (a late frame just moves a
        little further). Displays without ``realtime`` (the GIF recorder, tests) get exact
        one-pixel steps instead."""
        first = anim.first_frame()
        if not anim.scrolls:
            self.show(first, anim.duration)
            return
        self.show(first, anim.scroll_start)              # transition in + read the start
        realtime = getattr(self.display, "realtime", True)
        step = anim.frame_interval
        clock = time.monotonic
        start = clock() - anim.scroll_start
        px = 1                                           # pixel 0 is the first frame, already shown
        t = anim.scroll_start + step
        while t < anim.scroll_end and not self.reload_pending:
            t0 = clock()
            frame = anim.frame_at(t)
            if realtime:
                self.display.show(frame, 0)
                self.frame_times.append(clock() - t0)
                # sleep until the text is due to move by the next pixel
                elapsed = clock() - start
                nxt = anim.scroll_start + (int((elapsed - anim.scroll_start) / step) + 1) * step
                wait = nxt - (clock() - start)
                if wait > 0:
                    time.sleep(wait)
                t = clock() - start
            else:
                self.display.show(frame, step)
                self.frame_times.append(clock() - t0)
                px += 1
                t = anim.scroll_start + px * step            # exact pixel steps, no drift
        last = anim.last_frame()
        self.display.show(last, max(0.0, anim.duration - anim.scroll_end))
        self.current = last

    def play_frames(self, anim):
        """Show a frame-by-frame animation (the arcade scenes). It comes in with the normal
        transition and its last frame is where the next transition starts. On a real panel
        frames follow the clock: if the Pi falls behind, a frame is skipped rather than the
        animation slowing down."""
        step = 1.0 / anim.fps
        self.show(anim.first_frame(), step)
        frames = anim.frames
        realtime = getattr(self.display, "realtime", True)
        clock = time.monotonic
        start = clock()
        for i, frame in enumerate(frames[1:], 1):
            if self.reload_pending:
                break
            if not realtime:
                self.display.show(frame, step)
                continue
            due = start + i * step
            if clock() > due + step and i < len(frames) - 1:
                continue                        # running late: drop this frame, keep the pace
            t0 = clock()
            self.display.show(frame, 0)
            self.frame_times.append(clock() - t0)
            wait = due + step - clock()
            if wait > 0:
                time.sleep(wait)
        self.current = frames[-1]

    def transition(self, old, new, vertical=False, frames=14, duration=0.45):
        """Move the old screen out and the new one in over ``duration`` seconds with an
        ease-out curve: up and out the top (scroll) or left and out the side (slide)."""
        for frame in transition_frames(old, new, vertical, frames):
            self.display.show(frame, duration / frames)

    def slide(self, old, new, frames=14, duration=0.45):
        self.transition(old, new, vertical=False, frames=frames, duration=duration)

    def loading(self):
        for tick in range(4):
            self.display.show(self.renderer.loading(tick), 0.3)

    def step(self):
        """Show every asset once (stops early if settings changed)."""
        if self.reload_pending:
            self.reload()
        self.usage.tick()
        self.refresh()
        self.apply_brightness()
        if not self.assets and "coins" in self.cfg.screens:  # LOADING / NO DATA are about coins
            if self._fetching is not None and self.last_error is None:
                self.loading()  # first fetch still running
                return
            if self.last_error is not None:
                wait = max(5.0, min(30.0, self.next_fetch - time.time()))
                self.show(self.renderer.message("NO DATA", self.error_reason(), color=(230, 40, 30)),
                          hold=wait)
                if self.reload_pending:
                    return
        shown = 0
        for screen in self.screens():
            result = screen()
            if result is None:          # nothing to show right now (e.g. no headlines)
                continue
            if isinstance(result, Scroller):
                self.play(result)
            elif isinstance(result, Animation):
                self.play_frames(result)
            else:
                image, hold, *opts = result
                self.show(image, hold, slide=opts[0] if opts else True)
            shown += 1
            if self.reload_pending:
                return
        if not shown:  # nothing to show yet; wait for the retry without spinning
            self.wake.wait(1.0)

    def screens(self):
        """The screens for one pass of the cycle, in the configured order.
        Each item is a function returning (image, seconds to hold)."""
        out = []
        sleep = self.cfg.sleep
        for name in self.cfg.screens:
            if name == "clock":
                wx = self.weather.update()
                out.append(lambda wx=wx: (self.renderer.clock(
                    self.now(), wx, self.cfg.temp_unit, self.cfg.weather_place), sleep))
            elif name == "weather":
                wx = self.weather.update()
                # with extra cities, every weather screen is labeled with its city
                label = self.cfg.weather_place if self.extra_weather else ""
                if wx:  # no location or no data yet: skip the screen
                    out.append(lambda wx=wx, label=label: (
                        self.renderer.weather(wx, self.cfg.temp_unit, self.now(), label), sleep))
                for place, feed in self.extra_weather:
                    xwx = feed.update()
                    if xwx:
                        out.append(lambda xwx=xwx, place=place: (
                            self.renderer.weather(xwx, self.cfg.temp_unit, self.now(), place), sleep))
            elif name == "feargreed":
                fg = self.fear_greed.update()
                if fg:
                    out.append(lambda fg=fg: (self.renderer.fear_greed(fg), sleep))
            elif name == "indices":
                if self.cfg.indices_source == "yahoo":
                    data = self.yahoo.update()
                    rows = [{"label": r["label"], "change": r["change"], "spark": r["intraday"],
                             "abs": r["points"], "unit": "", "price": r.get("price"),
                             "prev_close": r.get("prev_close")} for r in (data or [])]
                else:
                    rows = list(self.index_rows)
                    dow = self.yahoo_dow.update()
                    if rows and dow:  # S&P, DOW, NAS order
                        rows.insert(1, {"label": "DOW", "change": dow[0]["change"],
                                        "abs": dow[0]["points"], "unit": "",
                                        "spark": dow[0]["intraday"], "price": dow[0].get("price"),
                                        "prev_close": dow[0].get("prev_close")})
                if rows and self.cfg.indices_style == "charts":
                    stale = self.stale and self.cfg.indices_source == "coingecko"
                    for row in rows:  # one screen per index
                        out.append(lambda row=row, stale=stale: (
                            self.renderer.index_chart(row, stale=stale), sleep))
                elif rows:
                    stale = self.stale and self.cfg.indices_source == "coingecko"
                    flip = self.cfg.indices_view == "both" and any(r.get("abs") is not None for r in rows)
                    hold = max(2.0, sleep / 2) if flip else sleep
                    out.append(lambda rows=rows: (self.renderer.indices(rows, tag=None, stale=stale), hold))
                    if flip:  # % first, then flip in place to the $ / point change
                        out.append(lambda rows=rows: (
                            self.renderer.indices(rows, tag=None, mode="abs", stale=stale), hold, False))
            elif name == "metals":
                rows = self.metals.update()
                if rows:
                    out.append(lambda rows=rows: (self.renderer.metals(rows), sleep))
            elif name == "watchlist":
                out += self.watchlist_screens()
            elif name == "news":
                if self.news.update():  # last good headlines; refreshed in the background
                    speed = SPEEDS[self.cfg.news_speed]
                    for item in self.news.next_batch(self.cfg.news_per_cycle):
                        out.append(lambda item=item: self.renderer.news(item, speed))
            elif name == "videos":
                if self.videos.update():  # last good uploads; refreshed in the background
                    speed = SPEEDS[self.cfg.news_speed]
                    for item in self.videos.next_batch(self.cfg.videos_per_cycle):
                        out.append(lambda item=item: self.renderer.news(item, speed, tag_rgb=VIDEO_TAG))
            elif name == "arcade":
                for anim in self.arcade.next_scenes(self.cfg.arcade_per_cycle):
                    out.append(lambda anim=anim: anim)
            elif name == "sports":
                self.sports.update()     # background refresh: 60 s while live, else 15 min
                now_ts = time.time()
                for g in self.sports.games(now_ts):
                    age = now_ts - g["fetched"]
                    stale = age > (3 * LIVE_EVERY if g["state"] == "in" else 2 * IDLE_EVERY)
                    out.append(lambda g=g, stale=stale: self.renderer.game(
                        g, LEAGUE_BY_KEY[g["league"]], self.tz, time.time(), hold=sleep, stale=stale))
                    if self.cfg.sports_odds and g["state"] == "pre" and g.get("odds"):
                        # the line flips in place after the game's own screen
                        out.append(lambda g=g, stale=stale: (self.renderer.game_odds(
                            g, LEAGUE_BY_KEY[g["league"]], stale=stale), max(2.0, sleep * 0.8), False))
            elif name == "odds":
                self.markets.update()    # background refresh every 5 minutes
                speed = SPEEDS[self.cfg.news_speed]
                for m in self.markets.next_batch(self.markets.rows(), self.cfg.odds_per_cycle):
                    out.append(lambda m=m: self.renderer.market(m, speed, stale=m["stale"], hold=sleep))
            elif name == "coins":
                out += self.coin_screens()
        return out

    def watchlist_screens(self):
        """The next WATCHLIST_PER_CYCLE symbols, one chart each; with List view on, list
        pages of the whole watchlist follow."""
        self.watchlist.update()      # background refresh; last good quotes meanwhile
        rows = self.watchlist.rows()
        sleep = self.cfg.sleep
        out = [lambda r=r: (self.renderer.quote(r, stale=r["stale"]), sleep)
               for r in self.watchlist.next_batch(rows, self.cfg.watchlist_per_cycle)]
        if self.cfg.list_view and rows:
            n = self.cfg.list_view
            items = [{"symbol": r["label"], "price": r["price"], "change_24h": r["change"],
                      "currency": r["currency"], "stale": r["stale"]} for r in rows]
            for i in range(0, len(items), n):
                page = items[i:i + n]
                out.append(lambda page=page: (self.renderer.list_page(
                    page, rows=n, stale=any(p["stale"] for p in page)), self.cfg.list_page_seconds))
        return out

    def coins_in_rotation(self):
        """(coins to show, starred coins). Focus narrows the rotation to starred coins;
        with Focus on but nothing starred, everything shows (the phone page says so)."""
        coins = list(self.assets)
        starred = [a for a in coins if a["symbol"].lower() in self.cfg.featured]
        if self.cfg.focus and starred:
            coins = starred
        return coins, starred

    def price_time(self):
        """When the prices on screen were fetched, in local time (None if never), for the
        "PRICES @ 08:00" screen before the coins. Stale prices keep their own fetch time."""
        ts = self.stale_since if self.stale and self.stale_since else self.last_success
        return datetime.fromtimestamp(ts, self.tz) if ts else None

    def coin_screens(self):
        sleep = self.cfg.sleep
        coins, starred = self.coins_in_rotation()
        starred_ids = {a["id"] for a in starred}
        out = []
        # prices are old (offline or the API failing): say so, and from when, before the coins
        summary = None  # shown before the coins and again after them
        if self.stale and self.stale_since and coins:
            when = datetime.fromtimestamp(self.stale_since, self.tz)
            summary = lambda when=when: (self.renderer.stale_notice(when, self.error_reason()),
                                         max(3.0, sleep))
        elif coins and self.price_time():  # live: when they were fetched, and which coins
            syms = [a["symbol"] for a in coins]
            summary = lambda when=self.price_time(), syms=syms: (
                self.renderer.prices_header(when, syms), max(3.0, sleep))
        if summary:
            out.append(summary)

        # In Focus mode every coin shown is starred, so big + regular screens doubled each
        # coin; there the big screen stands alone and gets the full time per coin.
        focus_only = bool(self.cfg.focus and starred)

        def big(asset):  # starred coins keep their big screens in every mode
            if focus_only:
                out.append(lambda a=asset: (self.renderer.featured(a, stale=self.stale), sleep))
                return
            out.append(lambda a=asset: (self.renderer.featured(a, stale=self.stale), max(2.0, sleep * 0.6)))
            out.append(lambda a=asset: (self.renderer.asset(a, stale=self.stale), sleep))

        # the normal rotation: every coin full screen (starred ones with their big screen)
        for asset in coins:
            if asset["id"] in starred_ids:
                big(asset)
            else:
                out.append(lambda a=asset: (self.renderer.asset(a, stale=self.stale), sleep))
        # List view adds the list pages at the end of each cycle, as a summary
        if self.cfg.list_view:
            n = self.cfg.list_view
            for i in range(0, len(coins), n):
                out.append(lambda page=coins[i:i + n]: (
                    self.renderer.list_page(page, rows=n, stale=self.stale), self.cfg.list_page_seconds))
        out += self.portfolio_screens()
        if summary:
            out.append(summary)
        return out

    def portfolio_screens(self):
        """The portfolio screen(s) after the coins, if switched on and something is held."""
        cfg = self.cfg
        if not (cfg.portfolio and cfg.holdings and self.assets):
            return []
        p = summarize(self.assets, cfg.holdings)
        if p is None:
            return []
        hold = max(3.0, cfg.sleep)
        if cfg.portfolio_private:
            return [lambda p=p: (self.renderer.portfolio(p, True, cfg.portfolio_pct), hold)]
        out = [lambda p=p: (self.renderer.portfolio(p, False, page="day"), hold)]
        if p["gain"] is not None:
            out.append(lambda p=p: (self.renderer.portfolio(p, False, page="cost"), hold))
        return out

    def run(self):
        self.apply_brightness()
        self.loading()
        while True:
            self.step()


def test_pattern(display, renderer, seconds=2.0):
    """Wiring check: solid red, green, blue, white, then a border with corner markers
    and the panel size. Wrong colors -> LED_RGB_SEQUENCE; missing rows/halves ->
    ribbon cable or LED_ROWS/LED_COLS/LED_CHAIN; flicker -> LED_SLOWDOWN_GPIO."""
    from PIL import ImageDraw
    w, h = display.width, display.height
    for name, color in (("RED", (255, 0, 0)), ("GREEN", (0, 255, 0)), ("BLUE", (0, 0, 255)),
                        ("WHITE", (255, 255, 255))):
        log.info("test pattern: all %s", name)
        display.show(Image.new("RGB", (w, h), color), seconds)
    img = renderer.message(f"{w}x{h}", "WIRING OK?")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w - 1, h - 1), outline=(255, 255, 255))
    for (x, y), c in zip(((0, 0), (w - 3, 0), (0, h - 3), (w - 3, h - 3)),
                         ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0))):
        d.rectangle((x, y, x + 2, y + 2), fill=c)  # red TL, green TR, blue BL, yellow BR
    log.info("test pattern: border + corners (red top-left, green top-right, blue bottom-left, yellow bottom-right)")
    display.show(img, seconds * 3)


def self_check():
    """Import everything the ticker needs and parse the settings, without touching the
    panel or the network. The updater runs this on new code before restarting."""
    import importlib
    for name in ("display", "feeds", "icons", "prices", "render", "session", "usage", "webui",
                 "cache", "retry", "logsetup", "sample", "updater", "launcher"):
        importlib.import_module(name)
    cfg = Config.from_env()
    Renderer(cfg.cols * cfg.chain, cfg.rows * cfg.parallel).message("OK")
    print("ticker self-check ok")
    return 0


def _mark_update_healthy_later(delay=60):
    """After a minute of running, tell the launcher the latest update is good."""
    def mark():
        try:
            import launcher
            if launcher.mark_healthy(HERE):
                log.info("update confirmed: running fine after %ds", delay)
        except Exception as exc:
            log.warning("could not record update health: %s", exc)
    t = threading.Timer(delay, mark)
    t.daemon = True
    t.start()


def _stop(signum, frame):
    raise SystemExit(0)  # unwinds to the finally below, which blanks the panel


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--emulate", action="store_true",
                        help="use RGBMatrixEmulator instead of the real panel")
    parser.add_argument("--emulator-adapter", default=None,
                        help="emulator output: browser (default, http://localhost:8888), pygame, "
                             "terminal, tkinter, sixel or raw")
    parser.add_argument("--sample", action="store_true",
                        help="show made-up prices and feed data (no network needed)")
    parser.add_argument("--check", action="store_true",
                        help="load settings and every module, then exit (used by the updater)")
    parser.add_argument("--test-pattern", action="store_true",
                        help="show a wiring test pattern on the panel and exit")
    parser.add_argument("--env", default=os.path.join(HERE, "settings.env"))
    args = parser.parse_args()

    if not args.check and os.path.exists(args.env):
        # every start: portfolio hidden and private, whatever was set before
        try:
            from webui import write_settings
            write_settings(args.env, {"PORTFOLIO": "off", "PORTFOLIO_PRIVATE": "on"})
        except (OSError, ValueError) as exc:
            log.warning("could not reset the portfolio switches: %s", exc)
    load_env_file(args.env)
    if args.check:
        return self_check()
    setup_logging()
    cfg = Config.from_env()
    signal.signal(signal.SIGTERM, _stop)  # systemctl stop -> clean shutdown

    from display import MatrixDisplay
    display = MatrixDisplay(cfg, emulate=args.emulate, adapter=args.emulator_adapter)
    if args.test_pattern:
        try:
            test_pattern(display, Renderer(display.width, display.height))
        finally:
            display.close()
        return
    ticker = Ticker(cfg, display, env_path=args.env)
    if args.sample:
        from sample import apply_sample
        apply_sample(ticker)
    if env("WEB_ENABLED", True, bool):
        import webui
        try:
            from updater import Updater
            webui.start(webui.WebApp(args.env, ticker, search=ticker.search, updater=Updater()),
                        port=env("WEB_PORT", 8080, int, lo=1, hi=65535))
        except OSError as exc:
            log.error("settings page could not start: %s", exc)
    log.info("ticker started (%s, %dx%d)", "emulator" if args.emulate else "LED panel",
             display.width, display.height)
    _mark_update_healthy_later()
    try:
        ticker.run()
    except KeyboardInterrupt:
        pass
    finally:
        ticker.request_cache.flush(force=True)
        display.close()
        log.info("ticker stopped")


if __name__ == "__main__":
    raise SystemExit(main())
