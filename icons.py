"""Coin icons for the LED panel.

Lookup order for each coin:
  1. icons/<symbol>.png           - your own override (any size, transparent ok)
  2. cache/icons/<coin-id>.png    - previously downloaded
  3. the icon URL CoinGecko returns with the price data
  4. the spothq/cryptocurrency-icons set on GitHub
  5. a generated round badge with the ticker's first letter
"""

import colorsys
import hashlib
import io
import logging
import os

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

log = logging.getLogger("ticker.icons")

HERE = os.path.dirname(os.path.abspath(__file__))
USER_DIR = os.path.join(HERE, "icons")
CACHE_DIR = os.path.join(HERE, "cache", "icons")
SPOTHQ = "https://raw.githubusercontent.com/spothq/cryptocurrency-icons/master/128/color/{}.png"


class IconStore:
    def __init__(self, size=16, download=True, timeout=10):
        self.size = size
        self.download = download
        self.timeout = timeout
        self._mem = {}
        os.makedirs(CACHE_DIR, exist_ok=True)

    def get(self, asset, font=None):
        key = asset["id"]
        if key not in self._mem:
            src = self._load_source(asset)
            self._mem[key] = self._fit(src) if src else self._badge(asset["symbol"], font)
        return self._mem[key]

    def _load_source(self, asset):
        sym = asset["symbol"].lower()
        for path in (os.path.join(USER_DIR, f"{sym}.png"),
                     os.path.join(CACHE_DIR, f"{asset['id']}.png")):
            if os.path.exists(path):
                try:
                    return Image.open(path).convert("RGBA")
                except OSError:
                    log.warning("could not read icon %s", path)
        if not self.download:
            return None
        for url in (asset.get("image"), SPOTHQ.format(sym)):
            if not url:
                continue
            try:
                r = requests.get(url, timeout=self.timeout)
                if r.status_code != 200:
                    continue
                img = Image.open(io.BytesIO(r.content)).convert("RGBA")
                img.save(os.path.join(CACHE_DIR, f"{asset['id']}.png"))
                log.info("downloaded icon for %s", sym)
                return img
            except (requests.RequestException, OSError) as exc:
                log.info("icon fetch failed for %s from %s: %s", sym, url, exc)
        return None

    def _fit(self, img):
        """Crop transparent margins, shrink to the icon box, flatten onto black,
        then sharpen and brighten so the logo reads on LEDs (dark colors
        otherwise almost vanish on a matrix panel)."""
        bbox = img.getchannel("A").getbbox()
        if bbox:
            img = img.crop(bbox)
        scale = self.size / max(img.width, img.height)
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                         Image.BOX)
        out = Image.new("RGB", (self.size, self.size), (0, 0, 0))
        out.paste(img, ((self.size - img.width) // 2, (self.size - img.height) // 2), img)
        out = out.filter(ImageFilter.UnsharpMask(radius=1, percent=150, threshold=0))
        peak = max(hi for _, hi in out.getextrema()) or 1
        out = out.point(lambda v: min(255, int(255 * (min(255, v * 255 / peak) / 255) ** 0.7)))
        return ImageEnhance.Color(out).enhance(1.4)

    def _badge(self, symbol, font):
        """Fallback: a colored disc with the ticker's first letter."""
        hue = int(hashlib.md5(symbol.encode()).hexdigest()[:2], 16) / 255
        r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.8, 0.9))
        s = self.size
        img = Image.new("RGB", (s, s), (0, 0, 0))
        ImageDraw.Draw(img).ellipse((0, 0, s - 1, s - 1), fill=(r, g, b))
        if font:
            letter = symbol[:1].upper()
            x = (s - font.width(letter)) // 2
            baseline = (s + font.ascent - font.descent) // 2 + 1
            font.draw(img, x, baseline, letter, (255, 255, 255))
        return img
