"""The emulator renders frames and writes PNG screenshots without LED hardware."""

import pytest

pytest.importorskip("RGBMatrixEmulator")

from PIL import Image  # noqa: E402

from display import MatrixDisplay  # noqa: E402
from sample import apply_sample  # noqa: E402
from ticker import Config, Ticker  # noqa: E402


def test_emulator_screenshots(tmp_path):
    cfg = Config()
    cfg.symbols = ["btc", "eth"]
    cfg.screens = ["coins"]
    cfg.download_icons = False
    cfg.transition = "none"
    cfg.sleep = 0
    cfg.brightness = 100
    display = MatrixDisplay(cfg, emulate=True, adapter="raw")
    t = Ticker(cfg, display)
    apply_sample(t)
    t.step()
    path = display.screenshot(str(tmp_path / "shot.png"))
    img = Image.open(path)
    assert img.size[0] == 64 * img.size[1] // 32 and img.size[0] >= 64
    assert img.convert("L").getextrema()[1] > 100  # something lit up


def test_screenshot_requires_emulator():
    d = MatrixDisplay.__new__(MatrixDisplay)
    d.emulated = False
    with pytest.raises(RuntimeError):
        d.screenshot("x.png")


def test_wiring_test_pattern(tmp_path):
    from render import Renderer
    from ticker import test_pattern

    cfg = Config()
    display = MatrixDisplay(cfg, emulate=True, adapter="raw")
    shown = []
    real_show = display.show
    display.show = lambda img, hold=0: (shown.append(img), real_show(img))
    test_pattern(display, Renderer(display.width, display.height), seconds=0)
    assert len(shown) == 5
    last = shown[-1]
    assert last.getpixel((0, 0)) == (255, 0, 0) and last.getpixel((63, 31)) == (255, 255, 0)
