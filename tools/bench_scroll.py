#!/usr/bin/env python3
"""Measure how long each scrolling frame takes, against the frame budget.

    python tools/bench_scroll.py              # emulator (raw adapter), every speed
    python tools/bench_scroll.py --panel      # on the Pi, with the real LED panel

At one pixel per frame the budget is 1/speed seconds (fast = 45 px/s -> 22 ms).
"build" is our Python work per frame (crop + paste + copy); "push" adds the display
call. In the emulator "push" is the emulator's own (slow, numpy) drawing; on the Pi
it's the LED library's C code. Run it on the Pi to see real numbers.
"""

import argparse
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from display import MatrixDisplay  # noqa: E402
from render import Renderer  # noqa: E402
from scroller import SPEEDS  # noqa: E402
from ticker import Config  # noqa: E402

HEADLINE = ("Bitcoin climbs past $109,000 as spot ETF inflows reach a three-week high and traders "
            "look ahead to the central bank's meeting minutes later this week")


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]


def measure(display, speed):
    anim = Renderer(display.width, display.height).news(
        {"title": HEADLINE, "source": "COINDESK", "at": time.time() - 3600}, speed=speed)
    build, total = [], []
    step = anim.frame_interval
    t = anim.scroll_start
    while t < anim.scroll_end:
        t0 = time.perf_counter()
        frame = anim.frame_at(t)
        t1 = time.perf_counter()
        display.show(frame, 0)
        t2 = time.perf_counter()
        build.append(t1 - t0)
        total.append(t2 - t0)
        t += step
    return anim, build, total


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--panel", action="store_true", help="use the real LED panel (on the Pi)")
    args = p.parse_args()
    cfg = Config()
    display = MatrixDisplay(cfg, emulate=not args.panel, adapter=None if args.panel else "raw")
    print(f"{'speed':8} {'budget':>8} {'frames':>7} {'build p50':>10} {'build p95':>10} "
          f"{'build max':>10} {'push p95':>10} {'headroom':>9}")
    worst = 0.0
    for name, speed in SPEEDS.items():
        anim, build, total = measure(display, speed)
        budget = anim.frame_interval
        p95 = pct(total, 0.95)
        worst = max(worst, p95 / budget)
        print(f"{name:8} {budget * 1000:7.1f}ms {len(build):7d} {statistics.median(build) * 1e3:9.2f}ms "
              f"{pct(build, 0.95) * 1e3:9.2f}ms {max(build) * 1e3:9.2f}ms {p95 * 1e3:9.2f}ms "
              f"{budget / max(p95, 1e-9):8.0f}x")
    display.close()
    return 0 if worst < 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
