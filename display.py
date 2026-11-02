"""Output backends: the real LED panel, the on-screen emulator, or a GIF file."""

import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EMULATOR_CONFIG = os.path.join(HERE, "emulator_config.json")
EMULATOR_ADAPTERS = ("browser", "pygame", "terminal", "tkinter", "sixel", "turtle", "raw")


def _emulator_modules(adapter=None, pixel_style=None):
    """Import RGBMatrixEmulator using this repo's emulator_config.json (the library
    otherwise reads and writes one in the current directory)."""
    # Importing the package already builds a default config (and writes the file if it's
    # missing), so import from this folder, where ours exists.
    cwd = os.getcwd()
    os.chdir(HERE)
    try:
        from RGBMatrixEmulator.internal.emulator_config import RGBMatrixEmulatorConfig
        RGBMatrixEmulatorConfig.CONFIG_PATH = EMULATOR_CONFIG
        from RGBMatrixEmulator import RGBMatrix, RGBMatrixOptions
    finally:
        os.chdir(cwd)

    def options():
        o = RGBMatrixOptions()
        if adapter:
            if adapter not in EMULATOR_ADAPTERS:
                raise ValueError(f"unknown emulator adapter {adapter!r}; use one of {EMULATOR_ADAPTERS}")
            from RGBMatrixEmulator.internal.adapter_loader import AdapterLoader
            o.display_adapter = AdapterLoader(adapter).load(fallback=False)
            if o.display_adapter is None:
                raise RuntimeError(f"emulator adapter {adapter!r} could not be loaded")
        if pixel_style:
            from RGBMatrixEmulator.internal.pixel_style import PixelStyle
            o.pixel_style = PixelStyle.fetch(pixel_style.upper())
        return o

    return RGBMatrix, options


class MatrixDisplay:
    """Drives the panel through hzeller's rpi-rgb-led-matrix (or its emulator)."""

    def __init__(self, cfg, emulate=False, adapter=None, pixel_style=None):
        self.emulated = emulate
        if emulate:
            RGBMatrix, RGBMatrixOptions = _emulator_modules(adapter, pixel_style)
        else:
            from rgbmatrix import RGBMatrix, RGBMatrixOptions
        o = RGBMatrixOptions()
        o.rows = cfg.rows
        o.cols = cfg.cols
        o.chain_length = cfg.chain
        o.parallel = cfg.parallel
        o.hardware_mapping = cfg.gpio_mapping
        o.gpio_slowdown = cfg.gpio_slowdown
        o.brightness = cfg.brightness
        o.pwm_bits = cfg.pwm_bits
        o.pwm_lsb_nanoseconds = cfg.pwm_lsb_nanoseconds
        o.led_rgb_sequence = cfg.rgb_sequence
        if cfg.panel_type:
            o.panel_type = cfg.panel_type
        if cfg.no_hardware_pulse:
            o.disable_hardware_pulsing = True
        o.drop_privileges = False  # we need to read icon/cache files after init
        self.matrix = RGBMatrix(options=o)
        self.canvas = self.matrix.CreateFrameCanvas()
        self.width, self.height = self.matrix.width, self.matrix.height

    wake = None  # threading.Event; lets a settings change interrupt a long hold
    realtime = True

    def show(self, image, hold=0.0):
        self.canvas.SetImage(image.convert("RGB"))
        self.canvas = self.matrix.SwapOnVSync(self.canvas)
        if hold:
            if self.wake is not None and hold > 0.2:
                self.wake.wait(hold)
            else:
                time.sleep(hold)

    def set_brightness(self, value):
        if self.matrix.brightness != value:
            self.matrix.brightness = value

    def screenshot(self, path):
        """Save what the emulator is showing as a PNG, LED-style (emulator only)."""
        if not self.emulated:
            raise RuntimeError("screenshots need --emulate")
        import numpy as np
        from PIL import Image
        adapter = self.canvas.display_adapter
        if hasattr(adapter, "frames") and adapter.frames:   # raw adapter: exact last frame
            pixels = adapter.frames[adapter.frame - 1]
        else:  # other adapters: re-render the canvas buffer the same way the emulator does
            pixels = self.canvas.options.screen.render(self.canvas._Canvas__pixels)
            pixels = (pixels * (self.canvas.brightness / 100.0)).astype(np.uint8)
        image = adapter._get_masked_image(pixels)
        if not isinstance(image, Image.Image):
            image = Image.fromarray(np.asarray(image, dtype=np.uint8))
        image.save(path)
        return path

    def close(self):
        self.matrix.Clear()


class GifDisplay:
    """Records frames instead of showing them; used by preview.py."""
    realtime = False  # scrolling screens step one pixel per frame instead of following the clock

    def __init__(self, width, height, scale=8):
        self.width, self.height, self.scale = width, height, scale
        self.frames, self.durations = [], []

    def show(self, image, hold=0.0):
        self.frames.append(image.copy())
        self.durations.append(max(20, int(hold * 1000)))

    def set_brightness(self, value):
        pass

    def save(self, path, pixel_gap=True):
        from PIL import Image, ImageDraw

        s = self.scale
        out = []
        for frame in self.frames:
            big = Image.new("RGB", (self.width * s, self.height * s), (12, 12, 12))
            draw = ImageDraw.Draw(big)
            px = frame.load()
            for y in range(self.height):
                for x in range(self.width):
                    c = px[x, y]
                    if c == (0, 0, 0):
                        c = (28, 28, 28)  # unlit LED
                    pad = 1 if pixel_gap else 0
                    draw.ellipse((x * s + pad, y * s + pad, x * s + s - 1 - pad, y * s + s - 1 - pad), fill=c)
            out.append(big)
        out[0].save(path, save_all=True, append_images=out[1:], duration=self.durations,
                    loop=0, optimize=False)
