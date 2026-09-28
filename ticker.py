#!/usr/bin/env python3
"""Crypto price ticker for an RGB LED matrix on a Raspberry Pi.

Run on the Pi:        sudo ./venv/bin/python ticker.py
Try it on a PC:       python ticker.py --emulate      (needs RGBMatrixEmulator)
Settings come from environment variables or settings.env (see settings.env.example).
"""

import argparse
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from PIL import Image

from zoneinfo import ZoneInfo

from feeds import FearGreed, Indices, parse_indices
from session import session_change
from icons import IconStore
from prices import CoinGecko, PriceError
from render import Renderer
from usage import Usage

HERE = os.path.dirname(os.path.abspath(__file__))

SCREEN_NAMES = ("clock", "feargreed", "indices", "coins")
# Tokenized index funds listed on CoinGecko: fetched in the same call as the coins
CG_INDICES = [("spx", "sp500-xstock", "S&P 500"), ("ndq", "nasdaq-xstock", "NASDAQ")]
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


def env(name, default, cast=str):
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    if cast is bool:
        return raw.lower() in ("1", "true", "yes", "on")
    return cast(raw)


@dataclass
class Config:
    symbols: list = field(default_factory=lambda: ["btc", "eth"])
    currency: str = "usd"
    api_key: str = ""
    api_pro: bool = False
    refresh_rate: int = 0          # seconds; 0 = auto (fit the monthly budget)
    monthly_budget: int = 10000
    sleep: float = 5
    transition: str = "slide"
    layout: str = "classic"
    featured: list = field(default_factory=list)
    screens: list = field(default_factory=lambda: list(SCREEN_NAMES))
    indices_source: str = "coingecko"
    indices: str = ""
    timezone: str = ""
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
        c.refresh_rate = 0 if rr in ("", "auto", "0") else max(60, int(rr))
        c.monthly_budget = env("MONTHLY_CALL_BUDGET", c.monthly_budget, int)
        c.sleep = env("SLEEP", c.sleep, float)
        c.transition = env("TRANSITION", c.transition).lower()
        c.layout = env("LAYOUT", c.layout).lower()
        c.featured = [s.strip().lower() for s in env("FEATURED", "").split(",") if s.strip()]
        screens = [s.strip().lower() for s in env("SCREENS", ",".join(SCREEN_NAMES)).split(",")]
        c.screens = [s for s in screens if s in SCREEN_NAMES] or ["coins"]
        c.indices_source = env("INDICES_SOURCE", c.indices_source).lower()
        c.indices = env("INDICES", "")
        c.timezone = env("TIMEZONE", "")
        c.brightness = env("BRIGHTNESS", c.brightness, int)
        c.dim_hours = env("DIM_HOURS", "")
        c.dim_brightness = env("DIM_BRIGHTNESS", c.dim_brightness, int)
        c.download_icons = env("DOWNLOAD_ICONS", True, bool)
        c.rows = env("LED_ROWS", c.rows, int)
        c.cols = env("LED_COLS", c.cols, int)
        c.chain = env("LED_CHAIN", c.chain, int)
        c.parallel = env("LED_PARALLEL", c.parallel, int)
        c.gpio_mapping = env("LED_GPIO_MAPPING", c.gpio_mapping)
        c.gpio_slowdown = env("LED_SLOWDOWN_GPIO", c.gpio_slowdown, int)
        c.pwm_bits = env("LED_PWM_BITS", c.pwm_bits, int)
        c.pwm_lsb_nanoseconds = env("LED_PWM_LSB_NANOSECONDS", c.pwm_lsb_nanoseconds, int)
        c.rgb_sequence = env("LED_RGB_SEQUENCE", c.rgb_sequence)
        c.panel_type = env("LED_PANEL_TYPE", "")
        c.no_hardware_pulse = env("LED_NO_HARDWARE_PULSE", False, bool)
        return c


def get_tz(name):
    try:
        return ZoneInfo(name) if name else None
    except Exception:
        log.warning("unknown TIMEZONE %r, using the Pi's clock setting", name)
        return None


def in_dim_window(spec, now=None):
    """spec like '22-7' (10pm to 7am). Empty spec means never dim."""
    if not spec or "-" not in spec:
        return False
    start, end = (int(p) % 24 for p in spec.split("-", 1))
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
        self.api = self._make_api(cfg)
        self.icons = IconStore(download=cfg.download_icons)
        self.renderer = Renderer(display.width, display.height, cfg.currency, self.icons,
                                 layout=cfg.layout)
        self.assets = []
        self.index_rows = []
        self.fear_greed = FearGreed()
        self.yahoo = Indices(parse_indices(cfg.indices))
        self.tz = get_tz(cfg.timezone)
        self.stale = False
        self.next_fetch = 0.0
        self.current = None

    def _cg_indices(self, cfg):
        return "indices" in cfg.screens and cfg.indices_source == "coingecko"

    def _make_api(self, cfg):
        symbols = list(cfg.symbols)
        if self._cg_indices(cfg):  # rides along in the same request: no extra calls
            symbols += [f"{sym}:{cid}" for sym, cid, _ in CG_INDICES]
        return CoinGecko(symbols, cfg.currency, cfg.api_key, cfg.api_pro,
                         on_call=self.usage.count_call)

    def now(self):
        return datetime.now(self.tz)

    def interval(self):
        """Seconds between price refreshes (fixed, or auto from the budget)."""
        return self.cfg.refresh_rate or self.usage.auto_interval()

    def refresh(self, now=None):
        now = now or time.time()
        if now < self.next_fetch:
            return
        try:
            fetched = self.api.fetch()
            index_ids = {cid for _, cid, _ in CG_INDICES} if self._cg_indices(self.cfg) else set()
            labels = {cid: label for _, cid, label in CG_INDICES}
            self.assets = [a for a in fetched if a["id"] not in index_ids]
            self.index_rows = []
            for a in fetched:
                if a["id"] in index_ids:
                    change, is_open = session_change(a["price"], a["sparkline"], a.get("updated"))
                    self.index_rows.append({
                        "label": labels[a["id"]], "spark": a["sparkline"], "open": is_open,
                        # fall back to the rolling 24h figure only if history is missing
                        "change": change if change is not None else (a["change_24h"] or 0.0)})
            self.stale = False
            self.next_fetch = now + self.interval()
            log.info("prices: %s", ", ".join(f"{a['symbol']} {a['price']:g}" for a in self.assets))
        except PriceError as exc:
            log.error("price fetch failed: %s", exc)
            self.stale = bool(self.assets)
            self.next_fetch = now + min(60, self.interval())
            if not self.assets:
                reason = "RATE LIMIT" if "429" in str(exc) else "NO NETWORK?"
                self.show(self.renderer.message("NO DATA", reason, color=(230, 40, 30)),
                          hold=min(30, self.interval()))

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
        if new.indices != old.indices:
            self.yahoo = Indices(parse_indices(new.indices))
        self.cfg = new
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

    def show(self, image, hold):
        if self.current is not None and self.cfg.transition == "slide":
            self.slide(self.current, image)
        self.display.show(image, hold)
        self.current = image

    def slide(self, old, new, frames=14, duration=0.45):
        w = self.display.width
        for i in range(1, frames):
            t = i / frames
            t = 1 - (1 - t) ** 3  # ease-out
            dx = round(w * t)
            frame = Image.new("RGB", old.size)
            frame.paste(old, (-dx, 0))
            frame.paste(new, (w - dx, 0))
            self.display.show(frame, duration / frames)

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
        shown = 0
        for screen in self.screens():
            image, hold = screen()
            self.show(image, hold)
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
                out.append(lambda: (self.renderer.clock(self.now()), sleep))
            elif name == "feargreed":
                fg = self.fear_greed.update()
                if fg:
                    out.append(lambda fg=fg: (self.renderer.fear_greed(fg), sleep))
            elif name == "indices":
                if self.cfg.indices_source == "yahoo":
                    data = self.yahoo.update()
                    rows = [{"label": r["label"], "change": r["change"], "spark": r["intraday"]}
                            for r in (data or [])]
                else:
                    rows = self.index_rows
                if rows:
                    out.append(lambda rows=rows: (self.renderer.indices(rows, tag=None), sleep))
            elif name == "coins":
                for asset in list(self.assets):
                    if asset["symbol"].lower() in self.cfg.featured:
                        out.append(lambda a=asset: (self.renderer.featured(a), max(2.0, sleep * 0.6)))
                    out.append(lambda a=asset: (self.renderer.asset(a, stale=self.stale), sleep))
        return out

    def run(self):
        self.apply_brightness()
        self.loading()
        while True:
            self.step()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--emulate", action="store_true",
                        help="use RGBMatrixEmulator instead of the real panel")
    parser.add_argument("--env", default=os.path.join(HERE, "settings.env"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env_file(args.env)
    cfg = Config.from_env()

    from display import MatrixDisplay
    display = MatrixDisplay(cfg, emulate=args.emulate)
    ticker = Ticker(cfg, display, env_path=args.env)
    if env("WEB_ENABLED", True, bool):
        import webui
        try:
            webui.start(webui.WebApp(args.env, ticker, search=ticker.search),
                        port=env("WEB_PORT", 8080, int))
        except OSError as exc:
            log.error("settings page could not start: %s", exc)
    try:
        ticker.run()
    except KeyboardInterrupt:
        pass
    finally:
        display.close()


if __name__ == "__main__":
    main()
