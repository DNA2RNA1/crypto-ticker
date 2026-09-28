#!/usr/bin/env python3
"""Crypto price ticker for an RGB LED matrix on a Raspberry Pi.

Run on the Pi:        sudo ./venv/bin/python ticker.py
Try it on a PC:       python ticker.py --emulate      (needs RGBMatrixEmulator)
Settings come from environment variables or settings.env (see settings.env.example).
"""

import argparse
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

from PIL import Image

from icons import IconStore
from prices import CoinGecko, PriceError
from render import Renderer

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("ticker")


def load_env_file(path):
    """Read KEY=value lines into os.environ (existing variables win)."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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
    refresh_rate: int = 600
    sleep: float = 5
    transition: str = "slide"
    layout: str = "classic"
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
        c.refresh_rate = max(60, env("REFRESH_RATE", c.refresh_rate, int))
        c.sleep = env("SLEEP", c.sleep, float)
        c.transition = env("TRANSITION", c.transition).lower()
        c.layout = env("LAYOUT", c.layout).lower()
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


def in_dim_window(spec, now=None):
    """spec like '22-7' (10pm to 7am). Empty spec means never dim."""
    if not spec or "-" not in spec:
        return False
    start, end = (int(p) % 24 for p in spec.split("-", 1))
    hour = (now or datetime.now()).hour
    return start <= hour < end if start < end else (hour >= start or hour < end)


class Ticker:
    def __init__(self, cfg, display):
        self.cfg = cfg
        self.display = display
        self.api = CoinGecko(cfg.symbols, cfg.currency, cfg.api_key, cfg.api_pro)
        self.icons = IconStore(download=cfg.download_icons)
        self.renderer = Renderer(display.width, display.height, cfg.currency, self.icons,
                                 layout=cfg.layout)
        self.assets = []
        self.stale = False
        self.next_fetch = 0.0
        self.current = None

    def refresh(self, now=None):
        now = now or time.time()
        if now < self.next_fetch:
            return
        try:
            self.assets = self.api.fetch()
            self.stale = False
            self.next_fetch = now + self.cfg.refresh_rate
            log.info("prices: %s", ", ".join(f"{a['symbol']} {a['price']:g}" for a in self.assets))
        except PriceError as exc:
            log.error("price fetch failed: %s", exc)
            self.stale = bool(self.assets)
            self.next_fetch = now + min(60, self.cfg.refresh_rate)
            if not self.assets:
                reason = "RATE LIMIT" if "429" in str(exc) else "NO NETWORK?"
                self.show(self.renderer.message("NO DATA", reason, color=(230, 40, 30)),
                          hold=min(30, self.cfg.refresh_rate))

    def apply_brightness(self):
        dim = in_dim_window(self.cfg.dim_hours)
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
        """Show every asset once."""
        self.refresh()
        self.apply_brightness()
        for asset in list(self.assets):
            self.show(self.renderer.asset(asset, stale=self.stale), self.cfg.sleep)

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
    try:
        Ticker(cfg, display).run()
    except KeyboardInterrupt:
        pass
    finally:
        display.close()


if __name__ == "__main__":
    main()
