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
            for name in ("5x8", "6x10", "6x12", "7x13")}


def trend_color(change):
    if change is None or abs(change) < 0.05:
        return FLAT
    return UP if change > 0 else DOWN


def price_candidates(price, currency):
    """Formatted price strings, most precise first."""
    sign = CURRENCY_SIGNS.get(currency, "")
    if price >= 1:
        return [f"{sign}{price:,.2f}", f"{sign}{price:,.0f}"]
    if price <= 0:
        return [f"{sign}0.00"]
    decimals = max(2, -math.floor(math.log10(price)) + 3)
    out = [f"{sign}{price:.{decimals}f}"]
    if decimals > 4:
        out.append(f"{sign}{price:.{decimals - 1}f}")
    return out


def change_text(change):
    if change is None:
        return "  --"
    arrow = "▲" if change >= 0 else "▼"
    return f"{arrow}{abs(change):.1f}%" if abs(change) < 100 else f"{arrow}{abs(change):.0f}%"


class Renderer:
    def __init__(self, width=64, height=32, currency="usd", icons=None):
        self.w, self.h = width, height
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
        for font in fonts:
            for text in candidates:
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

    # --- screens ----------------------------------------------------------
    def asset(self, asset, stale=False):
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
            end1 = f["7x13"].draw(img, tx, 13, asset["symbol"], SYMBOL)
            end2 = f["6x10"].draw(img, tx, 25, ctext, trend_color(change))
            font, text = self._fit_text(prices, [f["7x13"], f["6x12"], f["5x8"]], self.w - 2)
            font.draw(img, (self.w - font.width(text)) // 2, 42, text, PRICE)
            self._sparkline(img, asset.get("sparkline", []), (2, 47, self.w - 3, self.h - 3))
        else:
            if icon:
                img.paste(icon, (0, 0))
            tx = self.icon_size + 2
            end1 = f["7x13"].draw(img, tx, 10, asset["symbol"], SYMBOL)
            end2 = f["5x8"].draw(img, tx, 16, ctext, trend_color(change))
            sx = max(end1, end2) + 2
            self._sparkline(img, asset.get("sparkline", []), (sx, 1, self.w - 1, 14))
            font, text = self._fit_text(prices, [f["7x13"], f["6x12"], f["5x8"]], self.w)
            font.draw(img, (self.w - font.width(text)) // 2, self.h - 2, text, PRICE)

        if stale:  # prices couldn't be refreshed: small red marker bottom-right
            ImageDraw.Draw(img).rectangle((self.w - 2, self.h - 2, self.w - 1, self.h - 1), fill=DOWN)
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
