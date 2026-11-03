"""Arcade animations: the scenes, rotation, playback timing, transitions and settings."""

import time

import pytest

import arcade as ar
from test_ticker import FakeDisplay
from ticker import Config, Ticker


@pytest.mark.parametrize("make", ar.SCENES, ids=lambda m: m.__name__)
def test_scene_frames(make):
    a = make()
    assert 3.0 <= a.duration <= 8.0                         # short: a few seconds each
    assert all(f.size == (64, 32) and f.mode == "RGB" for f in a.frames)
    assert all(f.getbbox() for f in a.frames)                # never a blank frame (the maze is always there)
    moving = sum(a.frames[i].tobytes() != a.frames[i + 1].tobytes() for i in range(len(a.frames) - 1))
    assert moving > len(a.frames) * 0.5                      # it actually animates


def colors(img):
    return {img.getpixel((x, y)) for x in range(64) for y in range(32)}


def test_scene_contents():
    chase, power, cherries, hearts = (m() for m in ar.SCENES[:4])
    mid = chase.frames[len(chase.frames) // 2]
    assert {ar.YELLOW} <= colors(chase.frames[30]) and set(ar.GHOSTS.values()) <= colors(mid) | colors(chase.frames[90])
    assert ar.SCARED in colors(power.frames[70]) and ar.SCARED not in colors(power.frames[10])
    assert ar.CHERRY in colors(cherries.frames[5])
    assert ar.BOW in colors(hearts.frames[50]) and ar.HEART in colors(hearts.frames[60])


def test_sprites_stay_in_the_lane():
    """Characters move between the maze walls (rows 6-25); walls are rows 2-5 and 25-28."""
    for make in ar.SCENES:
        for f in make().frames:
            for x in range(64):
                for y in list(range(0, 2)) + list(range(29, 32)):
                    assert f.getpixel((x, y)) == (0, 0, 0), (make.__name__, x, y)


def test_rotation_and_caching():
    a = ar.Arcade()
    names = [s.name for s in a.next_scenes(1) + a.next_scenes(1) + a.next_scenes(2) + a.next_scenes(4)]
    assert names == ["chase", "power", "cherries", "sweethearts", "snack", "giant", "chase", "power"]
    again = a.next_scenes(6)
    assert [s.name for s in again] == ["cherries", "sweethearts", "snack", "giant", "chase", "power"]
    assert again[0] is a.next_scenes(6)[0]                   # built once, then reused
    assert len(a.next_scenes(9)) == 6                        # never more than there are


def make_ticker(screens=("arcade",), per_cycle=1, transition="scroll"):
    cfg = Config()
    cfg.screens = list(screens)
    cfg.arcade_per_cycle = per_cycle
    cfg.transition = transition
    t = Ticker(cfg, FakeDisplay())
    t.display.realtime = False
    return t


def test_screen_on_and_off():
    assert len(make_ticker(per_cycle=2).screens()) == 2
    assert make_ticker(screens=("clock",)).screens() and all(
        not isinstance(s(), ar.Animation) for s in make_ticker(screens=("clock",)).screens())


@pytest.mark.parametrize("transition,frames", [("scroll", 13), ("slide", 13), ("none", 0)])
def test_playback_with_transitions(transition, frames):
    from PIL import Image
    t = make_ticker(transition=transition)
    t.show(Image.new("RGB", (64, 32), (9, 9, 9)), 1)
    shown = []
    t.display.show = lambda img, hold=0: shown.append((img, hold))
    anim = ar.chase()
    t.play_frames(anim)
    assert len(shown) == frames + len(anim.frames)           # transition in, then every frame
    assert all(h == pytest.approx(1 / ar.FPS) for _, h in shown[frames:])
    assert t.current is anim.frames[-1]                      # the next transition starts here


def test_realtime_skips_frames_when_late(monkeypatch):
    t = make_ticker(transition="none")
    t.display.realtime = True
    shown = []
    t.display.show = lambda img, hold=0: shown.append(img)
    now = [1000.0]
    monkeypatch.setattr("ticker.time.monotonic", lambda: now[0])

    def slow_sleep(s):
        now[0] += s

    monkeypatch.setattr("ticker.time.sleep", slow_sleep)
    real_show = t.display.show

    def show_and_lag(img, hold=0):
        real_show(img, hold)
        now[0] += 0.12                                       # each frame takes 120 ms to push (budget 50 ms)

    t.display.show = show_and_lag
    anim = ar.cherry_bonus()
    t.play_frames(anim)
    assert len(shown) < len(anim.frames) / 2                 # frames dropped ...
    assert shown[-1] is anim.frames[-1]                      # ... but it still ends on the last one
    assert now[0] - 1000.0 < anim.duration + 0.5             # and takes about the normal time


def test_reload_stops_the_animation():
    t = make_ticker()
    shown = []

    def show(img, hold=0):
        shown.append(img)
        if len(shown) == 20:
            t.reload_pending = True

    t.display.show = show
    t.play_frames(ar.chase())
    assert len(shown) == 20


def test_settings(monkeypatch):
    assert Config.from_env().arcade_per_cycle == 1 and "arcade" not in Config.from_env().screens  # off by default
    monkeypatch.setenv("SCREENS", "clock,arcade,coins")
    monkeypatch.setenv("ARCADE_PER_CYCLE", "9")
    c = Config.from_env()
    assert c.screens == ["clock", "arcade", "coins"] and c.arcade_per_cycle == 6


def test_settings_page(app):
    s = app.settings()
    assert s["arcade_per_cycle"] == 1 and "arcade" in s["screen_names"]
    assert app.save({"screens": "arcade,clock", "arcade_per_cycle": 3}) == {
        "SCREENS": "arcade,clock", "ARCADE_PER_CYCLE": "3"}
    assert app.save({"arcade_per_cycle": 6}) == {"ARCADE_PER_CYCLE": "6"}
    for bad in (0, 7, "two"):
        with pytest.raises(ValueError):
            app.save({"arcade_per_cycle": bad})


def test_ghost_snack():
    s = ar.ghost_snack()
    seen = [colors(f) for f in s.frames]
    caught = next(i for i, c in enumerate(seen) if ar.YELLOW not in c and i > 10)
    assert all(ar.SCARED in c or (255, 0, 0) in c for c in seen[:caught])    # a blue (or blinking) ghost
    assert (0, 255, 255) in seen[caught] and ar.YELLOW not in seen[caught]   # freeze: "200", chomper hidden
    later = seen[caught + 20]
    assert ar.PUPIL in later and ar.SCARED not in later and ar.YELLOW in later  # eyes going home


def test_giant():
    g = ar.giant()
    yellow = [sum(f.getpixel((x, y)) == ar.YELLOW for x in range(64) for y in range(32)) for f in g.frames]
    assert max(yellow[:56]) < 130 and max(yellow[56:]) > 350               # small first, then giant
    assert ar.GHOSTS["red"] in colors(g.frames[20]) and ar.SCARED in colors(g.frames[100])


@pytest.mark.parametrize("facing", [1, -1])
def test_giant_eye(facing):
    """The big chomper's eye: 3x3 dark pupil with a glint, up and forward like the small one."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (40, 40))
    ar.chomper(ImageDraw.Draw(img), 20, 20, 0, facing, r=12)
    ey = 20 - 8
    ex = 22 if facing > 0 else 16
    glint = (ex + 2, ey) if facing > 0 else (ex, ey)
    assert img.getpixel(glint) == ar.WHITE
    assert sum(img.getpixel((ex + dx, ey + dy)) == ar.PUPIL_DARK for dx in range(3) for dy in range(3)) == 8


def test_frame_build_is_cheap():
    """Scenes are built once and reused; building all of them should be quick."""
    t0 = time.perf_counter()
    for make in ar.SCENES:
        make()
    assert time.perf_counter() - t0 < 2.0


@pytest.mark.parametrize("facing,bow", [(1, False), (-1, False), (1, True), (-1, True)])
@pytest.mark.parametrize("t", [0, 2, 4])
def test_chomper_eye(facing, t, bow):
    """A 2x2 dark pupil with a white glint on the side it faces, in every mouth position;
    yellow around it (on Ms., her bow may touch the back of the pupil, never the front)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (24, 18))
    ar.chomper(ImageDraw.Draw(img), 12, 9, t, facing, bow=bow)
    ex, ey = (13, 5) if facing > 0 else (10, 5)
    glint = (ex + 1, ey) if facing > 0 else (ex, ey)
    pupil = [(ex + dx, ey + dy) for dx in (0, 1) for dy in (0, 1) if (ex + dx, ey + dy) != glint]
    assert img.getpixel(glint) == ar.WHITE
    assert all(img.getpixel(p) == ar.PUPIL_DARK for p in pupil)
    behind = (ex - 1, ey + 1) if facing > 0 else (ex + 2, ey + 1)     # the side away from the mouth
    above = (ex, ey - 1)
    ok = (ar.YELLOW, ar.BOW) if bow else (ar.YELLOW,)        # Ms.: the bow sits just behind the eye
    assert img.getpixel(behind) in ok and img.getpixel(above) in ok
    front = (glint[0] + 1, ey) if facing > 0 else (glint[0] - 1, ey)  # in front of the glint: her face
    assert img.getpixel(front) != ar.BOW
