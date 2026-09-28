"""Draws ticker screens as Pillow images sized to the LED panel."""

import math
import os

from PIL import Image, ImageDraw

from bdf import BDFFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")

UP = (0, 210, 90)
DOWN = (230, 40, 30)
FLAT = (150, 150, 150)
SYMBOL = (255, 200, 0)
PRICE = (255, 255, 255)
DIM = (90, 90, 90)

CURRENCY_SIGNS = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥", "cad": "$", "aud": "$"}


def load_fonts():
    return {name: BDFFont(os.path.join(FONT_DIR, f"{name}.bdf"))
            for name in ("4x6", "5x8", "6x10", "6x12", "7x13", "6x13B", "7x13B")}


def trend_color(change):
    if change is None or abs(change) < 0.05:
        return FLAT
    return UP if change > 0 else DOWN


SUBSCRIPT = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")


def price_candidates(price, currency):
    """Formatted price strings in order of preference.

    Under $1 we always keep 4 significant digits ($0.2412, $0.003410). Very
    small prices use the subscript-zero style exchanges use, so PEPE at
    0.00001123 becomes $0.0₄1123 (four zeros after the point, then 1123).
    """
    sign = CURRENCY_SIGNS.get(currency, "")
    if round(price, 2) >= 1000:
        return [f"{sign}{price:,.0f}"]
    if price >= 1:
        return [f"{sign}{price:,.2f}", f"{sign}{price:,.0f}"]
    if price <= 0:
        return [f"{sign}0.00"]

    def fmt(sig):
        decimals = max(2, -math.floor(math.log10(price)) - 1 + sig)
        frac = f"{price:.{decimals}f}".split(".")[1]
        zeros = len(frac) - len(frac.lstrip("0"))
        digits = frac.lstrip("0")[:sig]
        full = f"{sign}0.{frac}"
        short = f"{sign}0.0{str(zeros).translate(SUBSCRIPT)}{digits}" if zeros >= 4 else None
        return full, short

    full4, short4 = fmt(4)
    full3, short3 = fmt(3)
    if short4:
        return [short4, full4, short3]
    return [full4, full3]


def change_text(change):
    if change is None:
        return "  --"
    arrow = "▲" if change >= 0 else "▼"
    return f"{arrow}{abs(change):.1f}%" if abs(change) < 100 else f"{arrow}{abs(change):.0f}%"


class Renderer:
    LAYOUTS = ("classic", "chart", "mix")

    def __init__(self, width=64, height=32, currency="usd", icons=None, layout="classic"):
        self.w, self.h = width, height
        self.layout = layout if layout in self.LAYOUTS else "classic"
        self._count = 0
        self.currency = currency
        self.fonts = load_fonts()
        self.tall = height >= 64
        self.icon_size = 24 if self.tall else 16
        self.icons = icons
        if icons is not None:
            icons.size = self.icon_size

    def blank(self):
        return Image.new("RGB", (self.w, self.h), (0, 0, 0))

    # --- pieces -----------------------------------------------------------
    def _fit_text(self, candidates, fonts, max_width):
        """First candidate (most precise) that fits in any font, biggest font first."""
        for text in candidates:
            for font in fonts:
                if font.width(text) <= max_width:
                    return font, text
        return fonts[-1], candidates[-1]

    def _sparkline(self, img, points, box):
        x0, y0, x1, y1 = box
        width, height = x1 - x0 + 1, y1 - y0 + 1
        if len(points) < 2 or width < 8 or height < 4:
            return
        # resample to one value per column
        step = (len(points) - 1) / (width - 1)
        cols = [points[round(i * step)] for i in range(width)]
        lo, hi = min(cols), max(cols)
        span = (hi - lo) or 1.0
        color = trend_color((cols[-1] - cols[0]) / cols[0] * 100 if cols[0] else 0)
        faint = tuple(c // 5 for c in color)
        draw = ImageDraw.Draw(img)
        prev = None
        for i, v in enumerate(cols):
            y = y1 - round((v - lo) / span * (height - 1))
            x = x0 + i
            draw.line((x, y + 1, x, y1), fill=faint)  # soft fill under the line
            if prev is not None:
                draw.line((x - 1, prev, x, y), fill=color)
            prev = y
        draw.point((x1, prev), fill=(255, 255, 255))  # latest price

    def _outlined(self, img, font, x, baseline, text, color):
        """Text with a 1px black outline so it stays readable over the chart."""
        for dx, dy in ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)):
            font.draw(img, x + dx, baseline + dy, text, (0, 0, 0))
        return font.draw(img, x, baseline, text, color)

    def _area_chart(self, img, points, top, bottom):
        """Full-width 7-day chart: bright edge line, fill fading toward the bottom."""
        width = self.w
        if len(points) < 2:
            return
        step = (len(points) - 1) / (width - 1)
        cols = [points[round(i * step)] for i in range(width)]
        lo, hi = min(cols), max(cols)
        span = (hi - lo) or 1.0
        color = trend_color((cols[-1] - cols[0]) / cols[0] * 100 if cols[0] else 0)
        px = img.load()
        prev = None
        for x, v in enumerate(cols):
            y = bottom - round((v - lo) / span * (bottom - top))
            depth = max(1, bottom - y)
            for yy in range(y + 1, bottom + 1):
                k = 0.50 - 0.35 * (yy - y) / depth  # 50% under the line -> 15% at bottom
                px[x, yy] = tuple(int(c * k) for c in color)
            # edge line, filled vertically so steep moves stay connected
            a, b = (y, y) if prev is None else (min(prev, y), max(prev, y))
            for yy in range(a, b + 1):
                px[x, yy] = color
            prev = y

    # --- screens ----------------------------------------------------------
    def asset(self, asset, stale=False):
        layout = self.layout
        if layout == "mix":
            layout = ("classic", "chart")[self._count % 2]
        self._count += 1
        img = self._chart_screen(asset) if layout == "chart" else self._classic_screen(asset)
        if stale:  # prices couldn't be refreshed: small red marker bottom-right
            ImageDraw.Draw(img).rectangle((self.w - 2, self.h - 2, self.w - 1, self.h - 1), fill=DOWN)
        return img

    def _chart_screen(self, asset):
        """Chart fills the panel; symbol, change and price float on top."""
        img = self.blank()
        f = self.fonts
        change = asset.get("change_24h")
        ctext = change_text(change)
        prices = price_candidates(asset["price"], self.currency)
        big = self.tall
        sym_font = f["7x13B"] if big else f["6x13B"]
        chg_font = f["6x10"] if big else f["5x8"]
        top_base = 12 if big else 10
        # tall panels put the change on its own line under the symbol
        chg_base = top_base + 11 if big else top_base
        self._area_chart(img, asset.get("sparkline", []),
                         top=chg_base + 3 if big else top_base + 1, bottom=self.h - 1)
        cw = chg_font.width(ctext)
        # small coin icon in the top-left, unless it would push the symbol
        # into the 24h change on the right (long symbol + big move)
        isz = 12 if big else 10
        x = 1
        if self.icons and isz + 2 + sym_font.width(asset["symbol"]) + 2 <= self.w - cw - 1:
            img.paste(self.icons.get(asset, f["6x10"], size=isz), (0, 0))
            x = isz + 2
        self._outlined(img, sym_font, x, top_base, asset["symbol"], PRICE)
        self._outlined(img, chg_font, self.w - cw - 1, chg_base, ctext, trend_color(change))
        # "7D" tag in the bottom-right corner marks the chart's time span.
        # Fit the price in the space left of it; only if that's impossible
        # does the price get the full width and the tag is skipped.
        # Readable price beats the tag: keep the tag only if the price still
        # fits in a normal-size font next to it.
        tag_w = f["4x6"].width("7D")
        room = self.w - tag_w - 4
        font, text = self._fit_text(prices, [f["7x13"], f["6x12"]], room)
        show_tag = font.width(text) <= room and text == prices[0]
        if not show_tag:
            font, text = self._fit_text(prices, [f["7x13"], f["6x12"], f["5x8"]], self.w - 1)
        self._outlined(img, font, 1, self.h - 2, text, PRICE)
        if show_tag:
            self._outlined(img, f["4x6"], self.w - tag_w - 1, self.h - 1, "7D", (170, 170, 170))
        return img

    def _classic_screen(self, asset):
        img = self.blank()
        f = self.fonts
        icon = self.icons.get(asset, f["6x10"]) if self.icons else None
        change = asset.get("change_24h")
        ctext = change_text(change)
        prices = price_candidates(asset["price"], self.currency)

        if self.tall:
            if icon:
                img.paste(icon, (2, 2))
            tx = 2 + self.icon_size + 3
            end1 = f["7x13B"].draw(img, tx, 13, asset["symbol"], SYMBOL)
            end2 = f["6x10"].draw(img, tx, 25, ctext, trend_color(change))
            font, text = self._fit_text(prices, [f["7x13"], f["6x12"], f["5x8"]], self.w - 2)
            font.draw(img, (self.w - font.width(text)) // 2, 42, text, PRICE)
            self._sparkline(img, asset.get("sparkline", []), (2, 47, self.w - 3, self.h - 3))
        else:
            if icon:
                img.paste(icon, (0, 0))
            tx = self.icon_size + 2
            end1 = f["7x13B"].draw(img, tx, 10, asset["symbol"], SYMBOL)
            # symbol uses rows 1-9, change rows 12-18, price rows 21-30: 2px gaps
            end2 = f["5x8"].draw(img, tx, 18, ctext, trend_color(change))
            sx = max(end1, end2) + 2
            self._sparkline(img, asset.get("sparkline", []), (sx, 1, self.w - 1, 14))
            font, text = self._fit_text(prices, [f["7x13"], f["6x12"], f["5x8"]], self.w)
            font.draw(img, (self.w - font.width(text)) // 2, self.h - 2, text, PRICE)
        return img

    def message(self, title, detail="", color=SYMBOL):
        img = self.blank()
        f = self.fonts
        font = f["7x13"] if f["7x13"].width(title) <= self.w else f["5x8"]
        y = self.h // 2 + (2 if detail else 5)
        font.draw(img, (self.w - font.width(title)) // 2, y, title, color)
        if detail:
            d = detail[: self.w // 5]
            f["5x8"].draw(img, (self.w - f["5x8"].width(d)) // 2, y + 10, d, DIM)
        return img

    def loading(self, tick=0):
        return self.message("LOADING", "." * (tick % 4 + 1))
