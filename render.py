"""Draws ticker screens as Pillow images sized to the LED panel."""

import math
import os

from PIL import Image, ImageDraw

from bdf import BDFFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
AIRLINE_DIR = os.path.join(HERE, "icons", "airlines")   # your own airline logos, <ICAO>.png

UP = (0, 210, 90)
DOWN = (230, 40, 30)
FLAT = (150, 150, 150)
SYMBOL = (255, 200, 0)
PRICE = (255, 255, 255)
DIM = (90, 90, 90)
DIM_TEXT = (150, 150, 150)

CURRENCY_SIGNS = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥", "cad": "$", "aud": "$"}


def load_fonts():
    fonts = {name: BDFFont(os.path.join(FONT_DIR, f"{name}.bdf"))
             for name in ("4x6", "5x8", "6x10", "6x12", "7x13", "6x13B", "7x13B", "9x18B")}
    # 5x8 draws "." as a small plus centered on the baseline and "," with a tail below
    # it; on LEDs both look like "+" hanging under the line ("3.3%"). Redraw them to sit
    # on the baseline (row 7 of 8 is the baseline row; row 8 is the descender).
    f = fonts["5x8"]
    f.override(".", [".....", ".....", ".....", ".....", ".....", ".##..", ".##..", "....."])
    f.override(",", [".....", ".....", ".....", ".....", ".##..", ".##..", ".#...", "....."])
    # The larger fonts have the same problem (a plus-shaped "." in 6x10, 7x13, 6x13B and
    # 7x13B; commas hanging below the line in all of them): "$0.003410", "$109,235".
    for name, dot in (("6x10", 2), ("6x12", 2), ("7x13", 2), ("6x13B", 2), ("7x13B", 2), ("9x18B", 3)):
        baseline_punctuation(fonts[name], dot)
    return fonts


def baseline_punctuation(font, dot):
    """Redraw "." as a dot x dot square and "," as that square plus a 1-pixel tail,
    both ending on the baseline row like the digits (nothing in the descender)."""
    adv = font._glyph(".")[5]
    x = max(0, (adv - dot) // 2)
    sq = "." * x + "#" * dot + "." * (adv - x - dot)
    tail = "." * x + "#" + "." * (adv - x - 1)
    font.override(".", [sq] * dot, yoff=0)
    font.override(",", [sq] * dot + [tail], yoff=0)


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
    if short4:  # short3 can be None when rounding removes a zero (0.00009999 -> 0.000100)
        return [c for c in (short4, full4, short3) if c]
    return [full4, full3]


def change_text(change):
    if change is None:
        return "  --"
    arrow = "▲" if change >= 0 else "▼"
    return f"{arrow}{abs(change):.1f}%" if abs(change) < 100 else f"{arrow}{abs(change):.0f}%"



US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR",
}


def place_label(place):
    """'Edinburg, Texas, US' -> 'EDINBURG, TX'. Non-US regions are kept as written;
    a place with no region gives just the city."""
    parts = [p.strip() for p in (place or "").split(",") if p.strip()]
    if not parts:
        return ""
    city = parts[0]
    if len(parts) >= 2:
        region = parts[1]
        region = US_STATES.get(region.lower(), region)
        if region.lower() != city.lower():
            return f"{city}, {region}".upper()
    return city.upper()


class Renderer:
    LAYOUTS = ("classic", "chart", "mix")

    def __init__(self, width=64, height=32, currency="usd", icons=None, layout="classic"):
        self.w, self.h = width, height
        self.layout = layout if layout in self.LAYOUTS else "classic"
        self._count = 0
        self._logos = {}            # your airline pictures, fitted to the panel
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
        return self.stale_mark(img) if stale else img

    def stale_mark(self, img):
        """Prices couldn't be refreshed: a small red marker in the bottom-right corner
        means "this is the last good price, not a live one"."""
        w, h = self.w, self.h
        px = img.load()
        # a black ring keeps the dot visible on top of a (red) chart, but never erases text
        for x, y in [(w - 3, h - 3), (w - 2, h - 3), (w - 1, h - 3), (w - 3, h - 2), (w - 3, h - 1)]:
            if px[x, y] not in (PRICE, SYMBOL):
                px[x, y] = (0, 0, 0)
        ImageDraw.Draw(img).rectangle((w - 2, h - 2, w - 1, h - 1), fill=DOWN)
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
            # smaller chart, top-right, so the left column breathes
            self._sparkline(img, asset.get("sparkline", []), (sx, 2, self.w - 2, 12))
            # price at full size, at the very bottom
            font, text = self._fit_text(prices, [f["7x13"], f["6x12"], f["5x8"]], self.w)
            font.draw(img, (self.w - font.width(text)) // 2, self.h - 1, text, PRICE)
        return img

    def featured(self, asset, stale=False):
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
        return self.stale_mark(img) if stale else img

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

    def clock(self, now, weather=None, unit="F", place=""):
        """Big white time; weather icon and AM/PM top-right;
        bottom row: temperature (green) and day/date (orange)."""
        img = self._base()
        f = self.fonts
        hour = now.hour % 12 or 12
        t = f"{hour}:{now.minute:02d}"
        big = f["9x18B"]
        tw = big.width(t)
        big.draw(img, max(0, (45 - tw) // 2), 16, t, PRICE)
        # "CITY, ST", small, centered on the panel under the time
        city = place_label(place)
        if city:
            cf = f["4x6"]
            if cf.width(city) > 64 and ", " in city:
                city = city.split(", ")[0]  # drop the state before cutting the name
            while city and cf.width(city) > 64:
                city = city[:-1]
            cf.draw(img, max(0, (64 - cf.width(city)) // 2), 22, city, (130, 130, 140))
        # right column: weather icon, AM/PM under it
        if weather:
            self.weather_icon(img, 51, 0, weather, now)
        ampm = "AM" if now.hour < 12 else "PM"
        # one row up so it doesn't touch a centered "CITY, ST" underneath
        f["5x8"].draw(img, 63 - f["5x8"].width(ampm), 16, ampm, (170, 170, 170))
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
        return self._place(img)

    # --- weather screen ------------------------------------------------------
    HIGH = (255, 140, 40)
    LOW = (80, 170, 255)

    def weather(self, wx, unit="F", now=None, place=""):
        """Big current temperature with a large condition icon, today's high/low, and the
        forecast for +3, +6, +9 and +12 hours along the bottom."""
        from datetime import datetime
        now = now or datetime.now()
        img = self._base()
        f = self.fonts
        hi, lo = wx.get("high"), wx.get("low")
        hl = [] if hi is None or lo is None else [f"{round(hi)}", f"{round(lo)}"]
        # high/low column: a small dim "H"/"L" in front of each number
        tag_w = f["4x6"].width("H") + 1
        right = 64 - (max(f["5x8"].width(t) for t in hl) + tag_w + 2 if hl else 0)
        temp = f"{round(wx['temp'])}°"
        # biggest layout that fits: large icon first, then the clock-size icon;
        # the small unit letter goes last
        layout = None
        city = place_label(place).split(",")[0] if place else ""
        for scale, x in (((1, 15),) if city else ((2, 27), (1, 15))):
            for font in (f["9x18B"], f["7x13B"], f["6x13B"]):
                for unit_w in (f["5x8"].width(unit) + 1, 0):
                    if x + font.width(temp) + unit_w <= right:
                        layout = (scale, x, font, unit_w)
                        break
                if layout:
                    break
            if layout:
                break
        scale, x, font, unit_w = layout or (1, 15, f["6x13B"], 0)
        icon = Image.new("RGB", (13, 10))
        self.weather_icon(icon, 0, 0, wx, now)
        if scale == 2:  # twice the clock's size, so it reads from across the room
            img.paste(icon.resize((26, 20), Image.NEAREST), (0, 0))
        elif city:  # several cities: the city's name over a small icon
            img.paste(icon, (0, 8))
            small = f["4x6"]
            name = city
            while name and small.width(name) > right - 2:
                name = name[:-1]
            small.draw(img, 0, 5, name, DIM_TEXT)
        else:
            img.paste(icon, (0, 4))
        tb = 18 if city else 16  # room for the city name above
        font.draw(img, x, tb, temp, PRICE)
        if unit_w:
            f["5x8"].draw(img, x + font.width(temp), tb - 7, unit, DIM_TEXT)
        # today's high (orange) and low (blue), right column, labeled H and L
        for y, t, color, letter in zip((7, 15), hl, (self.HIGH, self.LOW), ("H", "L")):
            x_num = 64 - f["5x8"].width(t)
            f["5x8"].draw(img, x_num, y, t, color)
            f["4x6"].draw(img, x_num - tag_w, y - 1, letter, DIM_TEXT)
        # next 12 hours, spelled out: the hour (dim) over its forecast temperature, every
        # 3 hours. The temperature's color runs from the low (blue) to the high (orange).
        pts = wx.get("hourly") or []
        steps = [h for h in (3, 6, 9, 12) if h < len(pts)]
        if steps:
            small = f["4x6"]
            vals = [pts[h] for h in steps]
            lo_v, hi_v = min(pts), max(pts)
            span = (hi_v - lo_v) or 1.0
            slot = 64 // len(steps)
            start_hour = wx.get("hour", now.hour)
            for i, (h, v) in enumerate(zip(steps, vals)):
                hour = (start_hour + h) % 24
                label = f"{hour % 12 or 12}{'A' if hour < 12 else 'P'}"
                t = (v - lo_v) / span
                color = tuple(int(self.LOW[j] + (self.HIGH[j] - self.LOW[j]) * t) for j in range(3))
                temp_txt = f"{round(v)}°"
                cx = i * slot + slot // 2
                small.draw(img, cx - small.width(label) // 2, 25, label, DIM_TEXT)
                small.draw(img, cx - small.width(temp_txt) // 2, 31, temp_txt, color)
        return self._place(img)

    # --- news screen -----------------------------------------------------------
    def tag(self, img, x, y, text, color, font=None):
        """Small colored label with dark text, e.g. a news source or league. Returns its width."""
        font = font or self.fonts["4x6"]
        w = font.width(text) + 3
        ImageDraw.Draw(img).rectangle((x, y, x + w - 1, y + font.ascent + 1), fill=color)
        font.draw(img, x + 2, y + font.ascent + 1, text, (0, 0, 0))
        return w

    @staticmethod
    def age(ts, now_ts):
        """"5M", "3H", "2D" for how old a headline is ('' if unknown)."""
        if not ts:
            return ""
        m = max(0, int((now_ts - ts) // 60))
        return "NOW" if m < 1 else f"{m}M" if m < 60 else f"{m // 60}H" if m < 48 * 60 else f"{m // 1440}D"

    def news(self, item, speed=30, now_ts=None, stale=False, tag_rgb=None):
        """One headline: source tag and age on top, the headline scrolling right to left
        below in a big bold font. The Videos screen reuses it with a red tag."""
        import time as _time
        from news import clean_text, tag_color
        from scroller import Lane, Scroller, text_strip
        f = self.fonts
        bg = Image.new("RGB", (self.w, self.h))
        top = (self.h - 32) // 2
        self.tag(bg, 0, top, item.get("source", "NEWS")[:12], tag_rgb or tag_color(item.get("source", "NEWS")))
        age = self.age(item.get("at"), now_ts or _time.time())
        if age:
            f["4x6"].draw(bg, self.w - f["4x6"].width(age), top + 6, age, DIM_TEXT)
        font = f["7x13B"]
        title = clean_text(item.get("title", ""), drawable=lambda ch: ord(ch) in font.glyphs)
        strip = text_strip(font, title, PRICE)
        lane = Lane(strip, (0, top + 14, self.w - 1, top + 14 + strip.height - 1))
        if stale:
            self.stale_mark(bg)
        return Scroller(bg, [lane], speed=speed, min_hold=3.0)

    # --- prediction markets (odds screen) ----------------------------------------
    def market(self, m, speed=30, stale=False, hold=5.0):
        """A prediction market: source tag with the question scrolling beside it, then the
        two leading outcomes with their implied probability and a bar (display only)."""
        from markets import TAG_COLORS
        from news import clean_text
        from scroller import Lane, Scroller, text_strip
        f = self.fonts
        bg = Image.new("RGB", (self.w, self.h))
        top = (self.h - 32) // 2
        color = TAG_COLORS.get(m.get("source"), (120, 120, 120))
        tag_w = self.tag(bg, 0, top, {"poly": "POLY", "kalshi": "KALSHI"}.get(m.get("source"), "ODDS"), color)
        lanes = []
        hf = f["5x8"]
        title = clean_text(m.get("title", "") + (" (closed)" if m.get("closed") else ""),
                           drawable=lambda ch: ord(ch) in hf.glyphs)
        strip = text_strip(hf, title, PRICE)
        lanes.append(Lane(strip, (tag_w + 2, top, self.w - 1, top + strip.height - 1)))
        dim = tuple(c // 3 for c in color)
        big = f["6x10"]
        for i, (name, p) in enumerate((m.get("outcomes") or [])[:2]):
            y = top + 9 + 12 * i
            pct = "<1%" if p < 0.005 else ">99%" if p > 0.995 else f"{p * 100:.0f}%"
            lead = i == 0
            ImageDraw.Draw(bg).line((0, y + 10, max(0, round(p * self.w) - 1), y + 10), fill=dim if not lead else color)
            big.draw(bg, self.w - big.width(pct), y + big.ascent - 1, pct, PRICE if lead else DIM_TEXT)
            room = self.w - big.width(pct) - 3
            name = clean_text(name.upper(), drawable=lambda ch: ord(ch) in big.glyphs) or "?"
            font = big if big.width(name) <= room else f["5x8"]
            ns = text_strip(font, name, PRICE if lead else DIM_TEXT, height=big.ascent + big.descent - 1)
            if ns.width <= room:
                bg.paste(ns, (0, y))
            else:
                lanes.append(Lane(ns, (0, y, room - 1, y + ns.height - 1)))
        if stale:
            self.stale_mark(bg)
        return Scroller(bg, lanes, speed=speed, min_hold=hold)

    # --- flights screen --------------------------------------------------------
    CLIMB = (0, 210, 90)
    DESCEND = (255, 140, 0)
    FLIGHT_DIM = (170, 175, 185)
    WAIT = (255, 170, 0)
    PROGRESS = (40, 140, 255)
    # Tile color (and text color) for well-known airlines: just their main brand color;
    # everyone else gets a color picked from the code
    AIRLINE_COLORS = {
        "UAL": ((0, 60, 190), (255, 255, 255)), "DAL": ((190, 20, 45), (255, 255, 255)),
        "AAL": ((0, 110, 200), (255, 255, 255)), "SWA": ((40, 70, 190), (255, 200, 0)),
        "JBU": ((0, 45, 140), (255, 255, 255)), "ASA": ((0, 90, 130), (255, 255, 255)),
        "NKS": ((240, 210, 0), (0, 0, 0)), "FFT": ((0, 120, 60), (255, 255, 255)),
        "AAY": ((240, 140, 0), (255, 255, 255)), "HAL": ((110, 40, 140), (255, 255, 255)),
        "SKW": ((30, 60, 120), (255, 255, 255)), "ENY": ((0, 110, 200), (255, 255, 255)),
        "RPA": ((0, 60, 190), (255, 255, 255)), "AMX": ((10, 30, 90), (255, 255, 255)),
        "VOI": ((180, 0, 60), (255, 255, 255)), "FDX": ((80, 20, 140), (255, 140, 0)),
        "UPS": ((90, 60, 30), (255, 190, 0)),
    }
    # A generic top-view airplane (original pixel art) for planes with no airline
    PLANE_ART = ("................", ".......##.......", ".......##.......", "......####......",
                 "......####......", ".....######.....", "..############..", "################",
                 "################", ".......##.......", ".......##.......", ".......##.......",
                 ".....######.....", "....########....", ".......##.......", "................")

    @staticmethod
    def hm(ts, tz=None):
        from datetime import datetime
        t = datetime.fromtimestamp(ts, tz)
        return t.strftime("%I:%M").lstrip("0") + ("A" if t.hour < 12 else "P")

    @staticmethod
    def flight_numbers(p, metric=False):
        """(distance, altitude, speed) strings for the panel."""
        d, alt, kt = p.get("dist_mi"), p.get("alt_ft"), p.get("speed_kt")
        if metric:
            dist = "" if d is None else (f"{d * 1.609344:.1f}KM" if d * 1.609344 < 10 else f"{d * 1.609344:.0f}KM")
            alt_s = "" if alt is None else f"{alt * 0.3048:,.0f}M"
            spd = "" if kt is None else f"{kt * 1.852:.0f}KMH"
        else:
            dist = "" if d is None else (f"{d:.1f}MI" if d < 10 else f"{d:.0f}MI")
            alt_s = "" if alt is None else f"{alt:,}FT"
            spd = "" if kt is None else f"{kt * 1.15078:.0f}MPH"
        return dist, alt_s, spd

    def airline_badge(self, icao, iata="", size=16):
        """The picture left of the text: your own logo file (icons/airlines/<ICAO>.png) if
        there is one, else a colored tile with the airline code, or a small plane for
        aircraft with no airline."""
        import colorsys
        import hashlib
        from icons import fit_logo
        if icao:
            path = os.path.join(AIRLINE_DIR, f"{icao}.png")
            try:
                key = (icao, size, os.path.getmtime(path))   # a new upload changes the time
                if key not in self._logos:
                    if len(self._logos) > 100:
                        self._logos.clear()
                    with Image.open(path) as im:
                        self._logos[key] = fit_logo(im.convert("RGBA"), size)
                return self._logos[key]
            except Exception:  # no file, or not a usable picture: the tile below
                pass
            color, ink = self.AIRLINE_COLORS.get(icao, (None, (255, 255, 255)))
            if color is None:
                hue = int(hashlib.md5(icao.encode()).hexdigest()[:2], 16) / 255
                color = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.75, 0.85))
            img = Image.new("RGB", (size, size))
            ImageDraw.Draw(img).rounded_rectangle((0, 0, size - 1, size - 1), radius=3, fill=color)
            code = iata or icao
            font = self.fonts["6x10"] if len(code) <= 2 else self.fonts["4x6"]
            font.draw(img, (size - font.width(code)) // 2 + 1, (size + font.ascent) // 2 - (0 if len(code) <= 2 else 1),
                      code, ink)
            return img
        img = Image.new("RGB", (size, size))
        px = img.load()
        for y, row in enumerate(self.PLANE_ART[:size]):
            for x, ch in enumerate(row[:size]):
                if ch == "#":
                    px[x, y] = (120, 190, 255)
        return img

    def _vertical_arrow(self, img, x, y, vrate):
        """3x5 arrow (the 4x6 font has none): up green when climbing, down orange when
        descending, nothing when level. Returns the width used."""
        if vrate is None or abs(vrate) < 300:
            return 0
        up = vrate > 0
        rows = (".#.", "###", ".#.", ".#.", ".#.") if up else (".#.", ".#.", ".#.", "###", ".#.")
        color = self.CLIMB if up else self.DESCEND
        px = img.load()
        for dy, row in enumerate(rows):
            for dx, ch in enumerate(row):
                if ch == "#" and 0 <= x + dx < img.width and 0 <= y + dy < img.height:
                    px[x + dx, y + dy] = color
        return 4

    @staticmethod
    def _panel_text(font, text):
        """Text from the web made safe for a panel font: accents dropped where the font
        lacks them (Aeroméxico -> Aeromexico), odd characters removed."""
        from news import clean_text
        return clean_text(text, drawable=lambda ch: ord(ch) in font.glyphs)

    def _flight_top(self, bg, lanes, top, badge, line1, line2, color2=PRICE):
        """Badge on the left, two lines of text beside it (each scrolls if too long)."""
        from scroller import Lane, text_strip
        bg.paste(badge, (0, top))
        f = self.fonts["5x8"]
        for text, base, color in ((line1, 7, PRICE), (line2, 15, color2)):
            text = self._panel_text(f, text)
            if not text:
                continue
            strip = text_strip(f, text, color)
            box = (18, top + base - f.ascent, self.w - 1, top + base - f.ascent + strip.height - 1)
            if strip.width <= self.w - 18:
                bg.paste(strip, box[:2])
            else:
                lanes.append(Lane(strip, box))

    def _line(self, bg, lanes, y_base, left, right="", lcolor=None, rcolor=DIM_TEXT):
        """A full-width 4x6 line: text on the left (scrolls if it doesn't fit), optional
        text on the right."""
        from scroller import Lane, text_strip
        f = self.fonts["4x6"]
        left = self._panel_text(f, left)
        rw = f.width(right) + 3 if right else 0
        if right:
            f.draw(bg, self.w - f.width(right), y_base, right, rcolor)
        if not left:
            return
        strip = text_strip(f, left, lcolor or self.FLIGHT_DIM)
        room = self.w - rw
        if strip.width <= room:
            bg.paste(strip, (0, y_base - f.ascent))
        else:
            lanes.append(Lane(strip, (0, y_base - f.ascent, room - 1, y_base - f.ascent + strip.height - 1)))

    def flight(self, p, metric=False, hold=5.0):
        """A plane near you, laid out like an airport board: airline picture on the left,
        airline and route beside it, then the aircraft type and distance, then altitude
        (with a climb/descent arrow) and ground speed."""
        from flights import airline_codes, compass, short_airline
        from scroller import SPEEDS, Scroller
        bg = Image.new("RGB", (self.w, self.h))
        top = (self.h - 32) // 2
        lanes = []
        route = p.get("route") or {}
        icao, iata = airline_codes(p.get("callsign", ""), route)
        ident = p.get("callsign") or p.get("reg") or "PLANE"
        if route.get("orig"):
            line1 = short_airline(route.get("airline")) or ident
            line2 = f"{route['orig']}-{route['dest']}"
            detail = " ".join(x for x in (p.get("type"), ident if line1 != ident else "") if x)
        else:
            line1 = ident
            line2 = p.get("type") or p.get("reg") or ""
            hdg = compass(p.get("track"))
            detail = (f"HDG {hdg}" if hdg else "") if not icao else (p.get("reg") or "")
        self._flight_top(bg, lanes, top, self.airline_badge(icao, iata), line1, line2)
        dist, alt, spd = self.flight_numbers(p, metric)
        self._line(bg, lanes, top + 24, detail, dist)
        x = self.fonts["4x6"].draw(bg, 0, top + 31, alt, PRICE)
        self._vertical_arrow(bg, x + 1, top + 26, p.get("vrate_fpm"))
        if spd:
            self.fonts["4x6"].draw(bg, self.w - self.fonts["4x6"].width(spd), top + 31, spd, DIM_TEXT)
        return Scroller(bg, lanes, speed=SPEEDS["normal"], min_hold=hold)

    def tracked_flight(self, r, tz=None, hold=5.0):
        """A flight you're following: airline picture, flight number and route, then
        where it is (or LANDED / NOT DEPARTED / NO SIGNAL), then a progress bar with the
        estimated arrival time."""
        from flights import airline_codes
        from scroller import SPEEDS, Scroller
        bg = Image.new("RGB", (self.w, self.h))
        top = (self.h - 32) // 2
        lanes = []
        route = r.get("route") or {}
        icao, iata = airline_codes(r.get("callsign", ""), route)
        line2 = f"{route['orig']}-{route['dest']}" if route.get("orig") else "ROUTE ?"
        self._flight_top(bg, lanes, top, self.airline_badge(icao or "???", iata), r["flight"], line2,
                         PRICE if route.get("orig") else DIM_TEXT)
        st = r.get("status")
        if st == "air":
            text, color = (f"OVER {r['place'].upper()}" if r.get("place") else "IN THE AIR"), PRICE
        elif st == "landed":
            text, color = f"LANDED {self.hm(r['landed_at'], tz)}" if r.get("landed_at") else "LANDED", self.CLIMB
        elif st == "ground":
            text, color = "ON THE GROUND", self.WAIT
        elif st == "lost":
            text = f"NO SIGNAL {self.hm(r['seen_at'], tz)}" if r.get("seen_at") else "NO SIGNAL"
            color = self.WAIT
        elif st == "checking":
            text, color = "CHECKING", DIM_TEXT
        else:
            text, color = ("AIRLINE NOT FOUND" if r.get("error") == "airline not found" else "NOT DEPARTED"), self.WAIT
        self._line(bg, lanes, top + 24, text, lcolor=color)
        f = self.fonts["4x6"]
        right = ""
        if st == "air" and r.get("eta"):
            right = "ETA " + self.hm(r["eta"], tz)
        elif st == "landed":
            right = "ARRIVED"
        elif r.get("progress") is not None:
            right = f"{r['progress'] * 100:.0f}%"
        rw = f.width(right) + 3 if right else 0
        if right:
            f.draw(bg, self.w - f.width(right), top + 31, right, self.CLIMB if st == "landed" else PRICE)
        x1 = self.w - rw - 1
        d = ImageDraw.Draw(bg)
        d.rectangle((0, top + 27, x1, top + 29), outline=(60, 60, 70))
        prog = r.get("progress")
        if prog is not None and st in ("air", "landed", "lost"):
            fill = self.CLIMB if st == "landed" else self.PROGRESS if st == "air" else (110, 110, 120)
            end = round(1 + prog * (x1 - 2))
            if end >= 1:
                d.line((1, top + 28, end, top + 28), fill=fill)
            if st == "air":
                d.point((end, top + 27), fill=PRICE)
                d.point((end, top + 29), fill=PRICE)
                d.point((min(x1 - 1, end + 1), top + 28), fill=PRICE)
        return Scroller(bg, lanes, speed=SPEEDS["normal"], min_hold=hold)

    # --- sports screen ---------------------------------------------------------
    LIVE = (255, 60, 50)
    UPCOMING = (255, 200, 0)
    STAR = (255, 200, 0)

    @staticmethod
    def star(img, x, y, color=(255, 200, 0)):
        """5x5 star (the BDF fonts have no ★) with its top-left at (x, y)."""
        rows = ("..#..", "#####", ".###.", ".#.#.", "#...#")
        px = img.load()
        for dy, row in enumerate(rows):
            for dx, ch in enumerate(row):
                if ch == "#" and 0 <= x + dx < img.width and 0 <= y + dy < img.height:
                    px[x + dx, y + dy] = color

    @staticmethod
    def game_status(game, tz=None, now_ts=None):
        """(text, color) for the top line: live clock, FINAL, or the start day/time."""
        from datetime import datetime
        import time as _time
        st = game["state"]
        detail = (game.get("detail") or "").upper().replace(" - ", " ")
        if st == "in":
            return detail or "LIVE", Renderer.LIVE
        if st == "post":
            return (detail or "FINAL").replace("FINAL/", "F/"), DIM_TEXT
        start = datetime.fromtimestamp(game["start"], tz)
        now = datetime.fromtimestamp(now_ts or _time.time(), tz)
        hm = start.strftime("%I:%M").lstrip("0") + ("A" if start.hour < 12 else "P")
        days = (start.date() - now.date()).days
        if days == 0:
            return hm, Renderer.UPCOMING
        if 0 < days < 7:
            return f"{start.strftime('%a').upper()} {hm}", Renderer.UPCOMING
        return f"{start.month}/{start.day} {hm}", Renderer.UPCOMING

    def team_line(self, team, fav, game):
        """One team as a strip: color block, #rank, abbreviation, star, and the score
        (or the record before the game). Returns (left part, right part) images."""
        from sports import team_color
        f = self.fonts
        big, small = f["6x10"], f["4x6"]
        h = big.ascent + big.descent
        base = big.ascent
        rank = f"#{team['rank']}" if team.get("rank") else ""
        w = 4 + (small.width(rank) + 1 if rank else 0) + big.width(team["abbr"]) + (6 if fav else 0)
        left = Image.new("RGB", (w, h))
        ImageDraw.Draw(left).rectangle((0, 0, 2, base - 1), fill=team_color(team))
        x = 4
        if rank:
            small.draw(left, x, base, rank, DIM_TEXT)
            x += small.width(rank) + 1
        x = big.draw(left, x, base, team["abbr"], PRICE)
        if fav:
            self.star(left, x + 1, base - 7, self.STAR)
        st = game["state"]
        other = game["home"] if team is game["away"] else game["away"]
        if st == "pre":
            text, color, font = (team.get("record") or ""), DIM_TEXT, f["5x8"]
        else:
            s, o = team.get("score"), other.get("score")
            text = "" if s is None else str(s)
            leading = s is not None and o is not None and s >= o
            color, font = (PRICE if leading else DIM_TEXT), big
        right = Image.new("RGB", (max(1, font.width(text)), h))
        if text:
            font.draw(right, 0, base, text, color)
        return left, right

    def game(self, game, league, tz=None, now_ts=None, hold=5.0, stale=False):
        """A game: league tag and status on top, then the away and home teams. Anything
        that doesn't fit its line scrolls (the news scroller)."""
        from scroller import SPEEDS, Lane, Scroller, text_strip
        f = self.fonts
        bg = Image.new("RGB", (self.w, self.h))
        top = (self.h - 32) // 2
        tag_w = self.tag(bg, 0, top, league.name, league.color)
        lanes = []
        status, color = self.game_status(game, tz, now_ts)
        sf = f["5x8"]
        room = self.w - tag_w - 2
        if sf.width(status) <= room:
            sf.draw(bg, self.w - sf.width(status), top + 7, status, color)
        else:
            strip = text_strip(sf, status, color)
            lanes.append(Lane(strip, (tag_w + 2, top, self.w - 1, top + strip.height - 1)))
        for y, side in ((top + 9, "away"), (top + 21, "home")):
            team = game[side]
            left, right = self.team_line(team, game.get("fav", {}).get(side), game)
            if left.width + 3 + right.width <= self.w:
                bg.paste(left, (0, y))
                bg.paste(right, (self.w - right.width, y))
            else:  # too long for the panel: the whole line scrolls
                strip = Image.new("RGB", (left.width + 6 + right.width, left.height))
                strip.paste(left, (0, 0))
                strip.paste(right, (left.width + 6, 0))
                lanes.append(Lane(strip, (0, y, self.w - 1, y + strip.height - 1)))
        if stale:
            self.stale_mark(bg)
        return Scroller(bg, lanes, speed=SPEEDS["normal"], min_hold=hold)

    def game_odds(self, game, league, stale=False):
        """The betting line for an upcoming game, shown after its normal screen (display
        only): the total (O/U) on top, each team's spread on its row (the moneyline for
        baseball and hockey, or when there's no spread). None if the game has no line."""
        from sports import MONEYLINE_LEAGUES
        odds = game.get("odds")
        if not odds:
            return None
        f = self.fonts
        use_ml = odds.get("ml") and (league.key in MONEYLINE_LEAGUES or not odds.get("spread"))
        per_team = odds["ml"] if use_ml else odds.get("spread")
        img = Image.new("RGB", (self.w, self.h))
        top = (self.h - 32) // 2
        tag_w = self.tag(img, 0, top, league.name, league.color)
        heads = []
        if odds.get("total"):
            heads.append(f"O/U {odds['total']}")
        if use_ml:
            heads = [f"ML {h}" for h in heads] + heads + ["MONEYLINE", "ML"]
        elif per_team:
            heads += ["SPREAD", "LINE"]
        room = self.w - tag_w - 2
        fits = [(font, h) for h in heads for font in (f["5x8"], f["4x6"]) if font.width(h) <= room]
        if fits:  # the total first, in the bigger font if it fits
            font, head = fits[0]
            font.draw(img, self.w - font.width(head), top + (7 if font is f["5x8"] else 6), head, self.UPCOMING)
        for y, side in ((top + 9, "away"), (top + 21, "home")):
            left, _ = self.team_line(game[side], game.get("fav", {}).get(side), game)
            text = (per_team or {}).get(side) or ""
            font = f["6x10"] if left.width + 3 + f["6x10"].width(text) <= self.w else f["5x8"]
            room = self.w - (font.width(text) + 3 if text else 0)
            img.paste(left.crop((0, 0, min(left.width, room), left.height)), (0, y))
            if text:
                fav = text.startswith("-")
                font.draw(img, self.w - font.width(text), y + f["6x10"].ascent, text, PRICE if fav else DIM_TEXT)
        return self.stale_mark(img) if stale else img

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

    def indices(self, rows, tag="7D", mode="pct", stale=False):
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
        img = self._place(img)
        return self.stale_mark(img) if stale else img

    def index_chart(self, r, stale=False):
        """One index per screen: name and level, today's change in % and points, and a big
        chart of the day against yesterday's close (dotted line). The chart is green
        above the close and red below it, so the day's crossings show at a glance."""
        img = self._base()
        f = self.fonts
        color = trend_color(r["change"])
        pts = list(r.get("spark") or [])
        base = r.get("prev_close")
        if not base and pts and r.get("change") is not None:
            base = pts[-1] / (1 + r["change"] / 100)  # same close the % change is measured from
        # line 1: name (white) and level (or % if the level isn't known)
        f["5x8"].draw(img, 1, 7, r["label"], PRICE)
        pct = change_text(r["change"])
        level = r.get("price")
        if level:
            ltxt = f"{level:,.0f}" if level >= 1000 else f"{level:,.2f}"
            f["5x8"].draw(img, 64 - f["5x8"].width(ltxt), 7, ltxt, PRICE)
            line2 = [(pct, color)]
        else:
            f["5x8"].draw(img, 64 - f["5x8"].width(pct), 7, pct, color)
            line2 = []
        # line 2: % change left (if not above), points or $ change right
        absv = r.get("abs")
        atxt = self.abs_text(absv, r.get("unit", "")) if absv is not None else ""
        if line2:
            f["5x8"].draw(img, 1, 15, line2[0][0], color)
        elif atxt:
            f["4x6"].draw(img, 1, 14, "TODAY", DIM_TEXT)
        if atxt:
            f["5x8"].draw(img, 64 - f["5x8"].width(atxt), 15, atxt, color)
        if len(pts) >= 2 and base:
            self._day_chart(img, pts, base, (0, 18, 63, 31))
        img = self._place(img)
        return self.stale_mark(img) if stale else img

    def _day_chart(self, img, points, base, box):
        """Intraday chart against a reference level: dotted reference line, the area
        between line and reference filled green (above) or red (below)."""
        x0, y0, x1, y1 = box
        width, height = x1 - x0 + 1, y1 - y0 + 1
        step = (len(points) - 1) / (width - 1)
        cols = [points[min(len(points) - 1, round(i * step))] for i in range(width)]
        lo, hi = min(cols + [base]), max(cols + [base])
        pad = (hi - lo) * 0.08 or abs(base) * 0.001 or 1.0
        lo, hi = lo - pad, hi + pad
        def y_of(v):
            return y1 - round((v - lo) / (hi - lo) * (height - 1))
        yb = y_of(base)
        px = img.load()
        draw = ImageDraw.Draw(img)
        prev = None
        for i, v in enumerate(cols):
            x = x0 + i
            y = y_of(v)
            up = v >= base
            line = UP if up else DOWN
            fill = tuple(c // 4 for c in line)
            a, b = (y + 1, yb) if up else (yb + 1, y - 1)
            if a <= b:
                draw.line((x, a, x, b), fill=fill)
            if prev is not None:
                py, pup = prev
                draw.line((x - 1, py, x, y), fill=line if up == pup else (200, 200, 200))
            prev = (y, up)
        for x in range(x0, x1 + 1, 2):  # yesterday's close, dotted, drawn over the fill
            if px[x, yb] == (0, 0, 0) or px[x, yb] in (tuple(c // 4 for c in UP), tuple(c // 4 for c in DOWN)):
                px[x, yb] = (120, 120, 130)
        px[x1, prev[0]] = (255, 255, 255)  # latest

    def quote(self, r, stale=False):
        """One watchlist symbol: label and price, the day's change in % and points (or a
        NAV / CLOSED tag), and today's chart against yesterday's close. Mutual funds chart
        the last month of daily prices. Funds and stocks get a currency sign; futures and
        indices are plain numbers (points, cents per bushel, a yield)."""
        img = self._base()
        f = self.fonts
        color = trend_color(r["change"])
        font = f["5x8"]
        cur = r.get("currency") or ""
        prices = [p for p in self.compact_prices(r["price"], cur) if self._drawable(font, p)]
        whole = [p for p in prices if "K" not in p and "M" not in p] or prices
        label, text = r["label"], None
        for lab in (label, label[:5], label[:4]):
            text = next((p for p in whole if font.width(lab) + 3 + font.width(p) <= 64), None)
            if text:
                label = lab
                break
        if text is None:
            label = label[:4]
            text = next((p for p in prices if font.width(label) + 3 + font.width(p) <= 64), prices[-1])
        font.draw(img, 1, 7, label, SYMBOL)
        font.draw(img, 64 - font.width(text), 7, text, PRICE)
        font.draw(img, 1, 15, change_text(r["change"]), color)
        tag = "NAV" if r.get("kind") == "MUTUALFUND" else "CLOSED" if r.get("closed") else ""
        if tag:
            tw = f["4x6"].width(tag)
            f["4x6"].draw(img, 64 - tw, 14, tag, DIM_TEXT)
        else:
            pts = r.get("points")
            ptxt = self.abs_text(pts, "") if pts is not None else ""
            if ptxt and font.width(change_text(r["change"])) + 3 + font.width(ptxt) <= 64:
                font.draw(img, 64 - font.width(ptxt), 15, ptxt, color)
        pts = list(r.get("intraday") or [])
        if r.get("span") == "1M":
            if len(pts) >= 2:
                self._day_chart(img, pts, pts[0], (0, 18, 63, 31))
                f["4x6"].draw(img, 1, 23, "1M", DIM_TEXT)
        elif len(pts) >= 2 and r.get("prev_close"):
            self._day_chart(img, pts, r["prev_close"], (0, 18, 63, 31))
        img = self._place(img)
        return self.stale_mark(img) if stale else img

    def metals(self, rows, stale=False):
        """Gold, silver and oil. Up to two: name and price (green/red by today's change)
        with today's chart under each. Three: a price list, one row each."""
        img = self._base()
        f = self.fonts
        n = max(1, min(3, len(rows)))
        band = 32 // n
        top0 = (32 - band * n) // 2
        for i, r in enumerate(rows[:n]):
            top = top0 + i * band
            color = trend_color(r["change"])
            label = r["label"]
            prices = [p for p in self.compact_prices(r["price"], "usd") if "K" not in p] or \
                self.compact_prices(r["price"], "usd")
            text = next((p for p in prices if f["5x8"].width(label) + 3 + f["5x8"].width(p) <= 63),
                        None)
            if text is None:  # long name and long price: shorten the name, keep the price whole
                label, text = label[:4], prices[0]
            base = top + 7 if n <= 2 else top + (band + 7) // 2
            f["5x8"].draw(img, 1, base, label, PRICE)
            f["5x8"].draw(img, 63 - f["5x8"].width(text), base, text, color)
            if n <= 2:
                self._sparkline(img, r["intraday"], (1, top + 9, 62, top + band - 2))
        img = self._place(img)
        return self.stale_mark(img) if stale else img

    # --- list view: several coins per page ----------------------------------
    LIST_ROWS = (5, 4)

    @staticmethod
    def compact_prices(price, currency):
        """Shorter price strings for list rows, most precise first."""
        sign = CURRENCY_SIGNS.get(currency, "")
        full = list(price_candidates(price, currency))
        out = full + [p[len(sign):] for p in full if sign]           # $4,012 then 4,012
        for unit, div in (("M", 1e6), ("K", 1e3)):
            if round(price) >= div:  # 999.99 displays as 1,000, so it gets 1.0K too
                v = price / div
                short = f"{v:.1f}{unit}" if v < 10 else f"{v:.0f}{unit}"
                for t in (f"{v:.3g}{unit}", short):                   # $109K, 4.01K, 4.0K
                    out += [sign + t, t]
                break
        if price < 1:                                                   # .8123
            out += [p[len(sign):].replace("0.", ".", 1) for p in full]
            if 0 < price < 0.0001:                                      # .0₄11 (2 digits)
                frac = f"{price:.{-math.floor(math.log10(price)) + 1}f}".split(".")[1]
                zeros = len(frac) - len(frac.lstrip("0"))
                sub = f".0{str(zeros).translate(SUBSCRIPT)}"
                out += [sub + frac.lstrip("0")[:2], sub + frac.lstrip("0")[:1]]  # last resort
        seen = []
        return [p for p in out if not (p in seen or seen.append(p))]

    @staticmethod
    def _drawable(font, text):
        return all(ord(c) in font.glyphs for c in text)

    def list_page(self, assets, rows=5, stale=False):
        """One page of up to ``rows`` coins: symbol on the left, the full price on the
        right, colored green/red by the 24h change (no arrow or %).

        rows=5 uses the 4x6 font, rows=4 the 5x8 font. The price is kept whole as long as
        possible: full price, then without the currency sign, then a shorter symbol, and
        only as a last resort a shortened price (4,012 -> 4.0K).
        """
        rows = rows if rows in self.LIST_ROWS else 5
        img = self._base()
        font = self.fonts["4x6"] if rows == 5 else self.fonts["5x8"]
        line = 32 // rows                       # 6 or 8 pixels per row
        top = (32 - line * rows) // 2           # center the block vertically
        gap = 3 if rows == 5 else 2
        for i, a in enumerate(assets[:rows]):
            baseline = top + i * line + font.ascent
            color = trend_color(a.get("change_24h"))
            right = 61 if stale and i == rows - 1 else 64  # leave room for the stale dot
            cur = a.get("currency", self.currency)  # watchlist rows carry their own (or none)
            prices = [p for p in self.compact_prices(a["price"], cur) if self._drawable(font, p)]
            prices = prices or [f"{a['price']:.3g}"]
            whole = [p for p in prices if "K" not in p and "M" not in p] or prices[:1]
            full_sym = a["symbol"][:6]
            sym, text = full_sym, None
            # whole price first (with sign, then without), shortening the symbol before the price
            for symbol in (full_sym, full_sym[:5], full_sym[:4], full_sym[:3]):
                room = right - font.width(symbol) - gap
                text = next((p for p in whole if font.width(p) <= room), None)
                if text:
                    sym = symbol
                    break
            if text is None:  # last resort: K/M forms with the full symbol
                room = right - font.width(full_sym) - gap
                sym = full_sym
                text = next((p for p in prices if font.width(p) <= room), min(prices, key=font.width))
            font.draw(img, 0, baseline, sym, SYMBOL)
            font.draw(img, right - font.width(text), baseline, text, color)
        img = self._place(img)
        return self.stale_mark(img) if stale else img

    @staticmethod
    def _signed_pct(v):
        return f"{'+' if v >= 0 else '-'}{abs(v):.1f}%"

    @staticmethod
    def _signed_dollars(v):
        a = abs(v)
        body = f"{a:,.0f}" if a >= 100 else f"{a:,.2f}"
        return f"{'+' if v >= 0 else '-'}${body}"

    def portfolio(self, p, private=True, pct_mode="24h", page="day"):
        """Portfolio screen. Private: no dollar amounts, just a big % (24-hour, or since cost
        when a cost basis exists and pct_mode is "total"). Dollars: total value with the
        24-hour change (page "day") or the gain since cost (page "cost").
        Bottom row: today's top performer among the holdings."""
        img = self._base()
        f = self.fonts
        small = f["4x6"]
        small.draw(img, 1, 5, "PORTFOLIO", DIM_TEXT)
        use_cost = p.get("gain_pct") is not None and (
            (private and pct_mode == "total") or (not private and page == "cost"))
        tag = "TOTAL" if use_cost else "24H"  # TOTAL = gain since cost basis
        small.draw(img, 64 - small.width(tag), 5, tag, DIM_TEXT)
        pct = p["gain_pct"] if use_cost else p["day_pct"]
        color = trend_color(pct)
        if private:
            text = self._signed_pct(pct)
            font = next((fn for fn in (f["9x18B"], f["7x13B"], f["6x13B"]) if fn.width(text) <= 64), f["5x8"])
            font.draw(img, (64 - font.width(text)) // 2, 22, text, color)
        else:
            value = f"${p['value']:,.0f}" if p["value"] >= 100 else f"${p['value']:,.2f}"
            font = next((fn for fn in (f["7x13B"], f["6x13B"], f["6x10"], f["5x8"]) if fn.width(value) <= 64), f["5x8"])
            font.draw(img, (64 - font.width(value)) // 2, 17, value, PRICE)
            change = p["gain"] if use_cost else p["day_change"]
            left, right = self._signed_dollars(change), self._signed_pct(pct)
            if small.width(left) + 3 + small.width(right) > 64:
                left = ""
            small.draw(img, 1, 24, left, color)
            small.draw(img, 64 - small.width(right), 24, right, color)
        top = p.get("top")
        if top:
            sym, ch = top
            t1, t2 = f"TOP {sym[:6]} ", self._signed_pct(ch)
            x = (64 - small.width(t1 + t2)) // 2
            x = small.draw(img, x, 31, t1, DIM_TEXT)
            small.draw(img, x, 31, t2, trend_color(ch))
        return self._place(img)

    def prices_header(self, when, symbols):
        """Before and after the coins: when the prices were fetched and which coins are in
        the rotation, all in the small font, e.g. PRICES @ 08:00 / BTC ETH ADA SNEK / ..."""
        img = self._base()
        small = self.fonts["4x6"]
        label, stamp = "PRICES @ ", f"{when.hour:02d}:{when.minute:02d}"
        x = (64 - small.width(label + stamp)) // 2
        x = small.draw(img, x, 5, label, DIM_TEXT)
        small.draw(img, x, 5, stamp, PRICE)
        rows = 4  # 6-pixel lines under the title
        lines, line, shown = [], "", 0
        for sym in symbols:
            cand = f"{line} {sym}".strip()
            if small.width(cand) <= 63:
                line, shown = cand, shown + 1
                continue
            lines.append(line)
            if len(lines) == rows:
                line = ""
                break
            line, shown = sym, shown + 1
        if line and len(lines) < rows:
            lines.append(line)
        left = len(symbols) - shown
        if left > 0:  # make room for "+N" on the last line
            last = lines[-1]
            while last and small.width(f"{last} +{left}") > 63:
                last, left = (last.rsplit(" ", 1)[0] if " " in last else ""), left + 1
            lines[-1] = f"{last} +{left}".strip()
        for i, txt in enumerate(lines):
            small.draw(img, (64 - small.width(txt)) // 2, 12 + i * 6, txt, SYMBOL)
        return self._place(img)

    def stale_notice(self, when, reason=""):
        """Shown before the coins while prices can't be refreshed: what went wrong and when
        the prices on screen are from, e.g. NO NETWORK? / PRICES FROM / MON 11/2 10:41P."""
        img = self.blank()
        f = self.fonts
        top = (reason or "OFFLINE").upper()
        font = f["5x8"]
        font.draw(img, (self.w - font.width(top)) // 2, 8, top, DOWN)
        label = "PRICES FROM"
        f["4x6"].draw(img, (self.w - f["4x6"].width(label)) // 2, 16, label, DIM_TEXT)
        hour = when.hour % 12 or 12
        ampm = "A" if when.hour < 12 else "P"
        for stamp in (f"{when.strftime('%a').upper()} {when.month}/{when.day} {hour}:{when.minute:02d}{ampm}",
                      f"{when.month}/{when.day} {hour}:{when.minute:02d}{ampm}",
                      f"{hour}:{when.minute:02d}{ampm}"):
            if font.width(stamp) <= self.w:
                break
        font.draw(img, (self.w - font.width(stamp)) // 2, 27, stamp, PRICE)
        return self.stale_mark(img)

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
