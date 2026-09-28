"""Minimal BDF bitmap-font renderer for Pillow.

Pillow's own BDF loader only handles the first 256 characters, which drops the
euro sign and the up/down arrows. The fonts in ./fonts are full Unicode, so we
parse them directly.
"""

from PIL import Image


class BDFFont:
    def __init__(self, path):
        self.glyphs = {}  # codepoint -> (mask image, xoff, yoff, width, height, advance)
        self.ascent = 0
        self.descent = 0
        self._load(path)

    def _load(self, path):
        with open(path, encoding="latin-1") as fh:
            lines = iter(fh.read().splitlines())
        enc = adv = None
        bbx = None
        for line in lines:
            parts = line.split()
            if not parts:
                continue
            key = parts[0]
            if key == "FONT_ASCENT":
                self.ascent = int(parts[1])
            elif key == "FONT_DESCENT":
                self.descent = int(parts[1])
            elif key == "ENCODING":
                enc = int(parts[1])
            elif key == "DWIDTH":
                adv = int(parts[1])
            elif key == "BBX":
                bbx = tuple(int(p) for p in parts[1:5])
            elif key == "BITMAP":
                w, h, xoff, yoff = bbx
                rows = [next(lines).strip() for _ in range(h)]
                mask = Image.new("1", (max(w, 1), max(h, 1)), 0)
                px = mask.load()
                for y, row in enumerate(rows):
                    bits = int(row, 16) if row else 0
                    nbits = len(row) * 4
                    for x in range(w):
                        if bits & (1 << (nbits - 1 - x)):
                            px[x, y] = 1
                if enc is not None and enc >= 0:
                    self.glyphs[enc] = (mask, xoff, yoff, w, h, adv)
                enc = adv = bbx = None

    @property
    def height(self):
        return self.ascent + self.descent

    def width(self, text):
        return sum(self._glyph(c)[5] for c in text)

    def _glyph(self, char):
        return self.glyphs.get(ord(char)) or self.glyphs.get(ord("?"))

    def draw(self, image, x, baseline, text, color):
        """Draw text with its baseline at y=baseline. Returns the x after the text."""
        for char in text:
            mask, xoff, yoff, w, h, adv = self._glyph(char)
            top = baseline - yoff - h
            image.paste(color, (x + xoff, top, x + xoff + mask.width, top + mask.height), mask)
            x += adv
        return x
