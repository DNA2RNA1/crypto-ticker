"""Arcade animations: short maze-chase scenes for the panel (the "arcade" screen).

Original pixel art drawn here (not copied sprites): a yellow chomper, four ghosts, scared
blue ghosts, ghost eyes, cherries, a chomper with a red bow, and a giant chomper. Each scene is a list of 64x32 frames
played at FPS; the ticker shows them with the normal transition in and out.

Scenes rotate: each cycle shows the next ARCADE_PER_CYCLE scenes.
"""

import math
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

FPS = 20
W, H = 64, 32
LANE = 16                     # vertical centre of the sprites' lane

YELLOW = (255, 214, 0)
MAZE = (33, 33, 222)
DOT = (255, 184, 151)
WHITE = (255, 255, 255)
PUPIL_DARK = (0, 0, 0)        # the chomper's pupil
PUPIL = (33, 33, 222)
GHOSTS = {"red": (255, 0, 0), "pink": (255, 160, 200), "cyan": (0, 230, 230), "orange": (255, 160, 40)}
SCARED = (40, 40, 255)
SCARED_FACE = (255, 200, 200)
CHERRY = (230, 0, 30)
STEM = (60, 200, 60)
BOW = (255, 0, 60)
HEART = (255, 40, 90)


@dataclass
class Animation:
    """A ready-made sequence of frames (each shown for 1/FPS seconds)."""
    name: str
    frames: list = field(default_factory=list)
    fps: int = FPS

    @property
    def duration(self):
        return len(self.frames) / self.fps

    def first_frame(self):
        return self.frames[0]

    def last_frame(self):
        return self.frames[-1]


# --- sprites -------------------------------------------------------------------------
def chomper(d, x, y, t, facing=1, r=6, color=YELLOW, bow=False):
    """The yellow chomper centred at (x, y); its mouth opens and closes with t (frames)."""
    mouth = (0, 25, 45, 25)[t // 2 % 4]                   # degrees, half-angle
    start = (0 if facing > 0 else 180) + mouth
    end = (360 if facing > 0 else 540) - mouth
    if mouth:
        d.pieslice((x - r, y - r, x + r, y + r), start, end, fill=color)
    else:
        d.ellipse((x - r, y - r, x + r, y + r), fill=color)
    if bow:                                                # Ms. version: a red bow on the back of the head
        bx = x - 4 * facing                                # far enough back to leave room for the eye
        d.polygon([(bx - 4, y - r - 1), (bx, y - r + 2), (bx - 4, y - r + 4)], fill=BOW)
        d.polygon([(bx + 4, y - r - 1), (bx, y - r + 2), (bx + 4, y - r + 4)], fill=BOW)
        d.point((bx, y - r + 2), fill=BOW)
        d.point((x + 3 * facing, y + 2), fill=BOW)        # a touch of lipstick
    # the eye: a 2x2 dark pupil just behind the upper lip, with a white glint on the side
    # it's facing (a black eye alone is just unlit LEDs; the white LED makes it read)
    # (same spot at every size: a sixth of the radius forward, two thirds up; the giant's is 3x3)
    n = 2 if r < 10 else 3
    fwd = max(1, r // 6)
    ex = x + fwd if facing > 0 else x - fwd - n + 1
    ey = y - (2 * r) // 3
    d.rectangle((ex, ey, ex + n - 1, ey + n - 1), fill=PUPIL_DARK)
    d.point((ex + n - 1 if facing > 0 else ex, ey), fill=WHITE)


def ghost(d, x, y, t, color, look=1, scared=False, blink=False):
    """A ghost centred at (x, y): round top, wavy skirt that wiggles with t."""
    body = SCARED if scared else color
    if scared and blink:
        body = WHITE
    d.pieslice((x - 6, y - 6, x + 6, y + 6), 180, 360, fill=body)
    d.rectangle((x - 6, y, x + 6, y + 4), fill=body)
    feet = (0, 4, 8, 12) if t // 3 % 2 else (2, 6, 10)
    for fx in feet:                                        # the skirt's points
        d.polygon([(x - 6 + fx - 1, y + 5), (x - 6 + fx + 1, y + 5), (x - 6 + fx, y + 7)], fill=body)
    if scared:
        face = (255, 0, 0) if blink else SCARED_FACE
        d.rectangle((x - 3, y - 2, x - 2, y - 1), fill=face)
        d.rectangle((x + 2, y - 2, x + 3, y - 1), fill=face)
        for i, sx in enumerate(range(x - 4, x + 5, 2)):   # wobbly mouth
            d.point((sx, y + 2 + (i % 2)), fill=face)
        return
    for ex in (x - 3, x + 2):                              # eyes looking the way it moves
        d.rectangle((ex - 1, y - 3, ex + 1, y), fill=WHITE)
        d.rectangle((ex + (1 if look > 0 else -1), y - 2, ex + (1 if look > 0 else -1), y - 1), fill=PUPIL)


def eyes(d, x, y, look=-1):
    """Just a ghost's eyes (after it's been eaten), looking the way they travel."""
    for ex in (x - 3, x + 2):
        d.rectangle((ex - 1, y - 3, ex + 1, y), fill=WHITE)
        px = ex + (1 if look > 0 else -1)
        d.rectangle((px, y - 2, px, y - 1), fill=PUPIL)


def cherries(d, x, y):
    d.line((x - 2, y - 1, x + 1, y - 6), fill=STEM)
    d.line((x + 3, y, x + 1, y - 6), fill=STEM)
    d.line((x + 1, y - 6, x + 4, y - 7), fill=STEM)
    d.ellipse((x - 5, y - 2, x, y + 3), fill=CHERRY)
    d.ellipse((x + 1, y - 1, x + 6, y + 4), fill=CHERRY)
    d.point((x - 3, y), fill=WHITE)
    d.point((x + 3, y + 1), fill=WHITE)


def heart(d, x, y, size=1.0):
    r = max(1, round(2 * size))
    d.ellipse((x - 2 * r, y - r, x, y + r), fill=HEART)
    d.ellipse((x, y - r, x + 2 * r, y + r), fill=HEART)
    d.polygon([(x - 2 * r, y), (x + 2 * r, y), (x, y + 3 * r)], fill=HEART)


def maze(d):
    """Blue maze walls along the top and bottom of the lane."""
    for y in (4, 27):
        d.rounded_rectangle((-3, y - 2, W + 2, y + 1), radius=2, outline=MAZE)


_FONT = []


def tiny_number(img, x, y, text, color=(0, 255, 255)):
    """Score popup like "100", in the ticker's 4x6 font (loaded once, not per frame)."""
    if not _FONT:
        try:
            from bdf import BDFFont
            from render import FONT_DIR
            import os
            _FONT.append(BDFFont(os.path.join(FONT_DIR, "4x6.bdf")))
        except Exception:  # fonts missing (shouldn't happen): no popup
            _FONT.append(None)
    font = _FONT[0]
    if font:
        font.draw(img, x - font.width(text) // 2, y, text, color)


def _frame():
    img = Image.new("RGB", (W, H))
    return img, ImageDraw.Draw(img)


# --- scenes --------------------------------------------------------------------------
def chase():
    """The chomper eats a row of dots, four ghosts on its tail."""
    frames = []
    dots = list(range(4, W, 5))
    eaten = set()
    gap = 15
    for t in range(W + 4 * gap + 14):
        img, d = _frame()
        maze(d)
        x = t - 8
        eaten.update(dx for dx in dots if dx <= x + 2)
        for dx in dots:
            if dx not in eaten:
                d.rectangle((dx, LANE - 1, dx + 1, LANE), fill=DOT)   # 2x2: one pixel is too faint
        for i, color in enumerate(GHOSTS.values()):
            gx = x - gap * (i + 1)
            if -8 < gx < W + 8:
                ghost(d, gx, LANE - 1 + (t // 4 + i) % 2, t + i, color, look=1)
        if x < W + 8:
            chomper(d, x, LANE, t, 1)
        frames.append(img)
    return Animation("chase", frames)


def power_pellet():
    """A power pellet turns the ghosts blue; they turn and flee, the chomper chases them."""
    frames = []
    pellet_x = 40
    # part 1: ghosts chase the chomper to the right, toward the pellet
    for t in range(52):
        img, d = _frame()
        maze(d)
        x = t - 4
        if t % 8 < 6:
            d.ellipse((pellet_x - 2, LANE - 2, pellet_x + 2, LANE + 2), fill=DOT)
        for i, color in enumerate(list(GHOSTS.values())[:3]):
            ghost(d, x - 16 - 13 * i, LANE - 1, t + i, color, look=1)
        chomper(d, x, LANE, t, 1)
        frames.append(img)
    # part 2: ghosts scared, flee left; chomper turns around and follows
    for t in range(78):
        img, d = _frame()
        maze(d)
        x = 47 - int(t * 0.8)            # a bit slower than the ghosts: they get away
        for i in range(3):
            gx = 31 - 13 * i - int(t * 0.9)
            blink = t > 52 and t // 4 % 2 == 0
            ghost(d, gx, LANE - 1, t + i, None, look=-1, scared=True, blink=blink)
        chomper(d, x, LANE, t, -1)
        frames.append(img)
    return Animation("power", frames)


def cherry_bonus():
    """Blinking cherries in the middle; the chomper eats them and "100" pops up."""
    frames = []
    cx = 34
    for t in range(96):
        img, d = _frame()
        maze(d)
        x = t - 7
        eaten = x >= cx - 1
        if not eaten and (t // 5) % 2 == 0 or not eaten and t < 20:
            cherries(d, cx, LANE)
        if eaten and t - (cx + 8) < 26:
            tiny_number(img, cx, LANE + 2, "100")
        if x < W + 8:
            chomper(d, x, LANE, t, 1)
        frames.append(img)
    return Animation("cherries", frames)


def sweethearts():
    """The chomper and Ms. chomper (red bow) meet in the middle; a heart appears."""
    frames = []
    for t in range(100):
        img, d = _frame()
        maze(d)
        step = min(t, 26)
        left, right = -7 + step, W + 6 - step
        moving = t < 26
        chomper(d, left, LANE, t if moving else t // 2, 1)          # chomping slower once they've met
        chomper(d, right, LANE, t if moving else t // 2, -1, bow=True)
        if t >= 29:
            pulse = 1.0 + 0.5 * (math.sin((t - 29) / 2) > 0)     # a beating heart: two sizes
            rise = min(8, (t - 29) // 3)
            heart(d, W // 2, LANE - 2 - rise, pulse)
        frames.append(img)
    return Animation("sweethearts", frames)


def ghost_snack():
    """The chomper catches a blue ghost: "200" pops up, then the ghost's eyes race home."""
    frames = []
    caught_at, pause = None, 14
    gspeed = 0.55
    for t in range(104):
        img, d = _frame()
        maze(d)
        if caught_at is None:
            x, gx = t - 7, 18 + int(t * gspeed)
            if x + 4 >= gx - 4:                       # mouth reaches the ghost
                caught_at = t
        if caught_at is None:
            ghost(d, gx, LANE - 1, t, None, look=1, scared=True, blink=t > 24 and t // 3 % 2 == 0)
            chomper(d, x, LANE, t, 1)
        else:
            k = t - caught_at
            cx = caught_at - 7
            spot = 18 + int(caught_at * gspeed)
            if k < pause:                             # the classic freeze: score where the ghost was
                tiny_number(img, spot, LANE + 3, "200", (0, 255, 255) if k // 3 % 2 == 0 else WHITE)
            else:
                m = k - pause
                if spot - int(m * 2.5) > -10:         # eyes zip back home to the left, fast
                    eyes(d, spot - int(m * 2.5), LANE, look=-1)
                if cx + m < W + 8:
                    chomper(d, cx + m, LANE, t, 1)
        frames.append(img)
    return Animation("snack", frames)


def giant():
    """Intermission: a red ghost chases the chomper off to the left, then a giant chomper
    comes back the other way after a frightened blue ghost."""
    frames = []
    for t in range(56):                               # part 1: right to left, the ghost chasing
        img, d = _frame()
        maze(d)
        x = W + 8 - int(t * 1.6)
        chomper(d, x, LANE, t, -1)
        ghost(d, x + 16, LANE - 1, t, GHOSTS["red"], look=-1)
        frames.append(img)
    for t in range(74):                               # part 2: left to right, the tables turned
        img, d = _frame()
        maze(d)
        gx = -8 + int(t * 1.25)
        bx = -32 + int(t * 1.45)
        if gx < W + 8:
            ghost(d, gx, LANE - 1, t, None, look=1, scared=True)
        if bx < W + 14:
            chomper(d, bx, LANE, t // 2, 1, r=12)     # chomps at half speed: it's big
        frames.append(img)
    return Animation("giant", frames)


SCENES = [chase, power_pellet, cherry_bonus, sweethearts, ghost_snack, giant]


class Arcade:
    """Hands out the next scenes, rotating through all of them across cycles."""

    def __init__(self):
        self._next = 0
        self._cache = {}

    def next_scenes(self, n):
        out = []
        for _ in range(max(1, min(n, len(SCENES)))):
            make = SCENES[self._next % len(SCENES)]
            self._next += 1
            if make not in self._cache:      # build each scene once; frames are reused
                self._cache[make] = make()
            out.append(self._cache[make])
        return out
