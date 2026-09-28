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
DIM_TEXT = (150, 150, 150)

CURRENCY_SIGNS = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥", "cad": "$", "aud": "$"}


def load_fonts():
    return {name: BDFFont(os.path.join(FONT_DIR, f"{name}.bdf"))
            for name in ("4x6", "5x8", "6x10", "6x12", "7x13", "6x13B", "7x13B", "9x18B")}


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

    def featured(self, asset):
        """Splash for a favorite coin: big picture top-left, symbol and 24h
        change to its right, price along the bottom. Shown before the coin's
        regular price screen."""
        img = self.blank()
        f = self.fonts
        change = asset.get("change_24h")
        prices = price_candidates(asset["price"], self.currency)
        # price along the bottom, as big as fits (keeping full precision)
        pfonts = [f["6x10"], f["5x8"]] if not self.tall else [f["7x13"], f["6x12"], f["5x8"]]
        pfont, ptext = self._fit_text(prices, pfonts, self.w - 1)
        price_top = self.h - 1 - pfont.ascent  # first row the price can use
        # picture fills the space above the price, trimmed to its visible part
        box_h = price_top - 1
        box_w = min(box_h + 8, self.w // 2)
        if self.icons:
            pic = self.icons.get(asset, f["7x13B"], size=max(box_w, box_h))
            bbox = pic.getbbox()
            if bbox:
                pic = pic.crop(bbox)
            pic.thumbnail((box_w, box_h), Image.LANCZOS)
            img.paste(pic, ((box_w - pic.width) // 2, (box_h - pic.height) // 2))
        x = box_w + 2
        sym_font = f["7x13B"] if f["7x13B"].width(asset["symbol"]) <= self.w - x else f["6x13B"]
        sym_font.draw(img, x, 11, asset["symbol"], SYMBOL)
        f["5x8"].draw(img, x, 20, change_text(change), trend_color(change))
        pfont.draw(img, (self.w - pfont.width(ptext)) // 2, self.h - 1, ptext, PRICE)
        return img

    # --- extra screens (drawn on a 64x32 base, centered on bigger panels) ----
    def _base(self):
        return Image.new("RGB", (64, 32), (0, 0, 0))

    def _place(self, base):
        if base.size == (self.w, self.h):
            return base
        img = self.blank()
        img.paste(base, ((self.w - 64) // 2, (self.h - 32) // 2))
        return img

    # color themes through the day: (hour, top color, bottom color)
    CLOCK_THEMES = [(0, (80, 120, 255), (170, 60, 255)),    # night: blue -> violet
                    (6, (255, 150, 40), (255, 70, 120)),     # sunrise: orange -> pink
                    (10, (255, 230, 60), (255, 140, 0)),     # day: yellow -> orange
                    (17, (255, 90, 60), (200, 50, 200)),     # sunset: red -> magenta
                    (20, (120, 90, 255), (40, 170, 255))]    # evening: purple -> sky blue

    def _theme(self, hour):
        theme = self.CLOCK_THEMES[0]
        for t in self.CLOCK_THEMES:
            if hour >= t[0]:
                theme = t
        return theme[1], theme[2]

    def _gradient_text(self, img, font, x, baseline, text, top, bottom):
        """Draw text filled with a vertical color gradient."""
        mask = Image.new("RGB", img.size)
        end = font.draw(mask, x, baseline, text, (255, 255, 255))
        m = mask.convert("L")
        bbox = m.getbbox() or (0, baseline - font.ascent, 0, baseline)
        y0, y1 = bbox[1], bbox[3] - 1  # gradient spans the glyphs themselves
        grad = Image.new("RGB", img.size)
        gp = grad.load()
        for y in range(max(0, y0), min(img.height, y1 + 1)):
            t = (y - y0) / max(1, y1 - y0)
            c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
            for xx in range(img.width):
                gp[xx, y] = c
        img.paste(grad, (0, 0), m)
        return end

    @staticmethod
    def moon_age(now):
        """Days since new moon (0-29.5), from a known new moon and the synodic month."""
        from datetime import datetime, timezone
        ref = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
        ts = now if now.tzinfo else now.astimezone()
        return ((ts - ref).total_seconds() / 86400) % 29.530588853

    def _moon(self, img, cx, cy, r, now):
        """Moon phase disk in silver (Northern Hemisphere view)."""
        import math
        p = self.moon_age(now) / 29.530588853  # 0 new, 0.5 full
        k = math.cos(2 * math.pi * p)
        px = img.load()
        for y in range(-r, r + 1):
            for x in range(-r, r + 1):
                if x * x + y * y > r * r + r * 0.6:
                    continue
                ny, nx = y / r, x / r
                w = math.sqrt(max(0.0, 1 - ny * ny))
                lit = nx > k * w if p < 0.5 else nx < -k * w
                px[cx + x, cy + y] = (200, 210, 235) if lit else (30, 32, 50)

    # WMO weather codes (Open-Meteo) -> icon kind
    @staticmethod
    def weather_kind(code):
        if code == 0:
            return "clear"
        if code in (1, 2):
            return "partly"
        if code == 3:
            return "cloudy"
        if code in (45, 48):
            return "fog"
        if 71 <= code <= 77 or code in (85, 86):
            return "snow"
        if code >= 95:
            return "storm"
        if 51 <= code <= 67 or 80 <= code <= 82:
            return "rain"
        return "cloudy"

    def _cloud(self, d, x, y, color=(200, 205, 215)):
        """Small cloud with its top-left corner at (x, y); about 11x6."""
        d.ellipse((x + 2, y, x + 7, y + 5), fill=color)
        d.ellipse((x + 5, y + 1, x + 10, y + 5), fill=color)
        d.rectangle((x, y + 3, x + 10, y + 5), fill=color)

    def _sun(self, d, cx, cy, r=3, color=(255, 200, 0)):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0), (-1, -1), (1, 1), (-1, 1), (1, -1)):
            d.point((cx + dx * (r + 2), cy + dy * (r + 2)), fill=color)

    def weather_icon(self, img, x, y, wx, now):
        """Draw a weather icon (12 wide x 9 tall) with its top-left at (x, y)."""
        d = ImageDraw.Draw(img)
        kind = self.weather_kind(wx.get("code", 0))
        day = wx.get("is_day", True)
        if kind == "clear":
            if day:
                self._sun(d, x + 6, y + 4, r=2)
            else:
                self._moon(img, x + 6, y + 4, 4, now)
        elif kind == "partly":
            if day:
                d.ellipse((x + 1, y, x + 6, y + 5), fill=(255, 200, 0))
            else:
                self._moon(img, x + 4, y + 3, 3, now)
            self._cloud(d, x + 1, y + 3)
        elif kind == "cloudy":
            self._cloud(d, x + 1, y + 2)
        elif kind == "fog":
            for i, yy in enumerate((y + 2, y + 5, y + 8)):
                d.line((x + 1 + i % 2, yy, x + 11 - (i + 1) % 2, yy), fill=(150, 150, 160))
        else:
            self._cloud(d, x + 1, y, (170, 175, 190))
            if kind == "rain":
                for dx in (2, 5, 8):
                    d.line((x + 1 + dx, y + 7, x + dx, y + 8), fill=(60, 140, 255))
            elif kind == "snow":
                for dx, dy in ((2, 7), (5, 8), (8, 7)):
                    d.point((x + 1 + dx, y + dy), fill=(255, 255, 255))
            elif kind == "storm":
                d.line([(x + 6, y + 5), (x + 4, y + 7), (x + 7, y + 7), (x + 5, y + 8)], fill=(255, 220, 0))

    def clock(self, now, weather=None, unit="F"):
        """Big white time with live seconds; weather icon top-right;
        bottom row: temperature (green) and day/date (orange)."""
        img = self._base()
        f = self.fonts
        hour = now.hour % 12 or 12
        t = f"{hour}:{now.minute:02d}"
        big = f["9x18B"]
        tw = big.width(t)
        big.draw(img, max(0, (45 - tw) // 2), 16, t, PRICE)
        # right column: weather icon, AM/PM, seconds
        if weather:
            self.weather_icon(img, 51, 0, weather, now)
        f["4x6"].draw(img, 46, 16, "AM" if now.hour < 12 else "PM", (170, 170, 170))
        f["5x8"].draw(img, 54, 16, f"{now.second:02d}", (60, 200, 255))
        # bottom row
        font = f["5x8"]
        temp = f"{round(weather['temp'])}°{unit}" if weather else None
        side = font.width(temp) + 4 if weather else 0
        wd = now.strftime("%a").upper()
        for date in (f"{wd} {now.month}/{now.day}", f"{wd} {now.day}",
                     f"{now.month}/{now.day}", f"{now.day}"):
            if font.width(date) + side <= 64:
                break
        if weather:
            font.draw(img, 0, 30, temp, (60, 220, 90))
            font.draw(img, 64 - font.width(date), 30, date, (255, 140, 0))
        else:
            font.draw(img, (64 - font.width(date)) // 2, 30, date, (255, 140, 0))
        px = img.load()
        for xx in range(2, 62):
            px[xx, 21] = (40, 40, 40)
        return self._place(img)

    FG_ZONES = [(24, (230, 40, 30)), (44, (255, 120, 0)), (55, (230, 200, 0)),
                (74, (120, 210, 60)), (100, (0, 210, 90))]

    def _fg_color(self, v):
        for top, color in self.FG_ZONES:
            if v <= top:
                return color
        return self.FG_ZONES[-1][1]

    def fear_greed(self, fg):
        """Speedometer-style gauge: red-to-green arc lit up to today's value,
        white needle, big number, mood on top, change vs yesterday and last week."""
        import math
        img = self._base()
        f = self.fonts
        v = max(0, min(100, fg["value"]))
        color = self._fg_color(v)
        px = img.load()
        # gauge: semicircle centred at the bottom-left
        cx, cy, r_out, r_in = 16, 30, 15, 11
        for y in range(cy - r_out, cy + 1):
            for x in range(cx - r_out, cx + r_out + 1):
                d = math.hypot(x - cx, y - cy)
                if r_in <= d <= r_out + 0.3:
                    ang = math.degrees(math.atan2(cy - y, x - cx))  # 180 = left, 0 = right
                    val = (180 - ang) / 180 * 100
                    c = self._fg_color(round(val))
                    lit = val <= v + 0.5
                    px[x, y] = c if lit else tuple(ch // 5 for ch in c)
        # needle
        ang = math.radians(180 - v / 100 * 180)
        for i in range(0, r_out - 1):
            x = round(cx + math.cos(ang) * i)
            y = round(cy - math.sin(ang) * i)
            if 0 <= x < 64 and 0 <= y < 32:
                px[x, y] = (255, 255, 255)
        for dx in (-1, 0, 1):
            px[cx + dx, cy] = (255, 255, 255)
        # mood label across the top
        label = fg["label"].upper()
        lf = f["5x8"] if f["5x8"].width(label) <= 62 else f["4x6"]
        lf.draw(img, 63 - lf.width(label), 7 if lf is f["5x8"] else 6, label, color)
        # big number on the right
        num = str(v)
        nw = f["9x18B"].width(num)
        self._gradient_text(img, f["9x18B"], 63 - nw - 4, 24, num,
                            tuple(min(255, c + 60) for c in color), color)
        # change vs yesterday (D) and a week ago (W)
        hist = fg.get("history") or []
        parts = []
        for tag, back in (("D", 1), ("W", 7)):
            if len(hist) > back:
                d = v - hist[-1 - back]
                parts.append((f"{tag}{d:+d}", UP if d > 0 else DOWN if d < 0 else FLAT))
        x = 63 - sum(f["4x6"].width(t) for t, _ in parts) - 3 * max(0, len(parts) - 1)
        for t, c in parts:
            x = f["4x6"].draw(img, x, 31, t, c) + 3
        return self._place(img)

    @staticmethod
    def abs_text(value, unit):
        if value is None:
            return "--"
        mag = abs(value)
        num = f"{mag:,.2f}" if mag < 10 else f"{mag:,.1f}" if mag < 100 else f"{mag:,.0f}"
        return f"{'+' if value >= 0 else '-'}{unit}{num}"

    def indices(self, rows, tag="7D", mode="pct"):
        """Market page: one band per index with label, 24h change and a chart."""
        img = self._base()
        f = self.fonts
        n = max(1, min(3, len(rows)))
        band = 32 // n
        for i, r in enumerate(rows[:n]):
            top = i * band
            color = trend_color(r["change"])
            ctext = (self.abs_text(r.get("abs"), r.get("unit", "")) if mode == "abs"
                     else change_text(r["change"]))
            if n <= 2:  # label + change on one line, chart underneath
                f["5x8"].draw(img, 1, top + 7, r["label"], PRICE)
                f["5x8"].draw(img, 63 - f["5x8"].width(ctext), top + 7, ctext, color)
                self._sparkline(img, r["spark"], (1, top + 9, 62, top + band - 2))
            else:  # label, small chart, change on one line
                lw = f["5x8"].width(r["label"])
                cw = f["5x8"].width(ctext)
                f["5x8"].draw(img, 1, top + 8, r["label"], PRICE)
                f["5x8"].draw(img, 63 - cw, top + 8, ctext, color)
                self._sparkline(img, r["spark"], (lw + 3, top + 1, 62 - cw - 2, top + band - 2))
        if tag and n <= 2:
            tw = f["4x6"].width(tag)
            f["4x6"].draw(img, 63 - tw, 31, tag, (110, 110, 110))
        return self._place(img)

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
