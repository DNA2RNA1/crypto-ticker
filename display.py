"""Output backends: the real LED panel, the on-screen emulator, or a GIF file."""

import time


class MatrixDisplay:
    """Drives the panel through hzeller's rpi-rgb-led-matrix (or its emulator)."""

    def __init__(self, cfg, emulate=False):
        if emulate:
            from RGBMatrixEmulator import RGBMatrix, RGBMatrixOptions
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

    def show(self, image, hold=0.0):
        self.canvas.SetImage(image.convert("RGB"))
        self.canvas = self.matrix.SwapOnVSync(self.canvas)
        if hold:
            time.sleep(hold)

    def set_brightness(self, value):
        if self.matrix.brightness != value:
            self.matrix.brightness = value

    def close(self):
        self.matrix.Clear()


class GifDisplay:
    """Records frames instead of showing them; used by preview.py."""

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
