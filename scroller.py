"""Scrolling text for the LED panel (news headlines, sports rows that don't fit).

A ``Scroller`` is an animated screen: a static background plus one or more "lanes",
each a pre-rendered strip of text that moves right to left inside its box. Text that
fits its lane stays still. A lane that's too long shows its start for a moment,
scrolls until its end is in view, and holds there briefly.

Positions are computed from elapsed time, not frame count, so the speed stays the
same even if a frame is late; normally the text moves one pixel per frame.
Each frame is just a 64x32 copy plus a crop-and-paste per lane, so it stays far inside
the frame budget even on a Pi Zero (see tools/bench_scroll.py).
"""

from dataclasses import dataclass

from PIL import Image

SPEEDS = {"slow": 20, "normal": 30, "fast": 45}   # pixels per second
LEAD_IN = 1.0     # seconds the start of the text is shown before it moves
LEAD_OUT = 1.2    # seconds the end of the text is shown after it stops


@dataclass
class Lane:
    strip: Image.Image          # the whole text, rendered once
    box: tuple                  # (x0, y0, x1, y1) inclusive, where it's shown

    @property
    def width(self):
        return self.box[2] - self.box[0] + 1

    @property
    def travel(self):
        """Pixels the strip has to move so its end is in view (0 if it fits)."""
        return max(0, self.strip.width - self.width)


class Scroller:
    def __init__(self, background, lanes, speed=SPEEDS["normal"], min_hold=2.0):
        self.background = background.convert("RGB")
        self.lanes = list(lanes)
        self.speed = float(speed)
        travel = max((ln.travel for ln in self.lanes), default=0)
        moving = travel / self.speed
        self.scroll_start = LEAD_IN if travel else 0.0
        self.duration = max(min_hold, self.scroll_start + moving + (LEAD_OUT if travel else 0.0))
        self.frame_interval = 1.0 / self.speed           # one pixel per frame
        self.travel = travel

    @property
    def scrolls(self):
        return self.travel > 0

    def offset(self, lane, t):
        """How far ``lane`` has moved at time t (all lanes move together, each stops
        when its own end is in view)."""
        moved = (t - self.scroll_start) * self.speed
        return int(min(lane.travel, max(0.0, moved + 1e-6)))  # tolerate float error at whole pixels

    def frame_at(self, t):
        frame = self.background.copy()
        for lane in self.lanes:
            x0, y0, x1, y1 = lane.box
            off = self.offset(lane, t)
            piece = lane.strip.crop((off, 0, off + lane.width, lane.strip.height))
            frame.paste(piece, (x0, y0))
        return frame

    def first_frame(self):
        return self.frame_at(0.0)

    def last_frame(self):
        return self.frame_at(self.duration)

    @property
    def scroll_end(self):
        return self.scroll_start + self.travel / self.speed


def text_strip(font, text, color, height=None, pad_right=0):
    """Render ``text`` once into a strip as tall as the font (or ``height``)."""
    h = height or (font.ascent + font.descent)
    strip = Image.new("RGB", (max(1, font.width(text) + pad_right), h))
    font.draw(strip, 0, font.ascent, text, color)
    return strip
