"""The ticker loop: last good prices stay on screen, marked stale, and nothing crashes."""

import time

import pytest
from conftest import FakeResponse, malformed, rate_limited, timeout, wifi_down

from prices import NetworkError
from render import DOWN
from ticker import Config, Ticker


class FakeDisplay:
    width, height = 64, 32
    wake = None

    def __init__(self):
        self.frames = []
        self.brightness = None

    def show(self, image, hold=0.0):
        self.frames.append(image.copy())

    def set_brightness(self, value):
        self.brightness = value


def make_ticker(http):
    cfg = Config()
    cfg.symbols = ["btc", "eth", "ada:cardano"]
    cfg.screens = ["coins"]
    cfg.download_icons = False
    cfg.transition = "none"
    cfg.sleep = 0
    t = Ticker(cfg, FakeDisplay())
    t.api.session.get = http.get
    t.api.sleep = lambda s: None
    return t


def has_stale_marker(img):
    px = img.load()
    return px[img.width - 1, img.height - 1] == DOWN and px[img.width - 2, img.height - 2] == DOWN


def expire(t):
    """Pretend the next refresh is due and the response cache has aged out."""
    t.next_fetch = 0
    t.request_cache._mem.clear()


def test_normal_refresh(http, ok):
    t = make_ticker(http.add(ok))
    t.refresh(wait=True)
    assert [a["symbol"] for a in t.assets] == ["BTC", "ETH", "ADA"]
    assert not t.stale and t.failures == 0
    t.step()
    assert len(t.display.frames) == 3
    assert not any(has_stale_marker(f) for f in t.display.frames)


@pytest.mark.parametrize("failure", [timeout, wifi_down, rate_limited, malformed,
                                     lambda: FakeResponse(500, text="oops")],
                         ids=["timeout", "wifi-loss", "http-429", "malformed-json", "http-500"])
def test_failure_keeps_last_good_price_marked_stale(http, ok, failure):
    t = make_ticker(http.add(ok))
    t.refresh(wait=True)
    good = [a["price"] for a in t.assets]

    http.queue = [failure()]
    expire(t)
    t.refresh(wait=True)
    assert [a["price"] for a in t.assets] == good  # last good prices kept
    assert t.stale and t.failures == 1
    assert t.stale_since == pytest.approx(t.last_success)

    t.display.frames.clear()
    t.step()  # renders without raising
    assert t.display.frames and all(has_stale_marker(f) for f in t.display.frames)

    # recovery clears the stale flag
    http.queue = [ok]
    expire(t)
    t.refresh(wait=True)
    assert not t.stale and t.failures == 0 and t.stale_since is None


def test_backoff_grows_and_resets(http, ok, monkeypatch):
    monkeypatch.setattr("retry.random.random", lambda: 0.5)  # no jitter
    t = make_ticker(http.add(wifi_down()))
    delays = []
    for _ in range(6):
        expire(t)
        t.refresh(wait=True)
        delays.append(round(t.next_fetch - time.time()))
    assert delays == [30, 60, 120, 240, 480, 600]
    http.queue = [ok]
    expire(t)
    t.refresh(wait=True)
    assert t.failures == 0


def test_rate_limit_honours_retry_after(http, monkeypatch):
    monkeypatch.setattr("retry.random.random", lambda: 0.5)
    t = make_ticker(http.add(rate_limited("900")))
    t.refresh(wait=True)
    assert t.next_fetch - time.time() == pytest.approx(900, abs=2)
    assert t.error_reason() == "RATE LIMIT"


def test_settings_reload_does_not_cut_backoff_short(http, tmp_path):
    t = make_ticker(http.add(rate_limited("900")))
    t.refresh(wait=True)
    before = t.next_fetch
    env = tmp_path / "settings.env"
    env.write_text("SYMBOLS=btc,eth,ada:cardano\nSCREENS=coins\nBRIGHTNESS=40\n")
    t.env_path = str(env)
    t.reload()
    assert t.next_fetch == before


@pytest.mark.parametrize("failure,reason", [(wifi_down, "NO NETWORK?"), (rate_limited, "RATE LIMIT"),
                                            (malformed, "BAD DATA")])
def test_cold_start_without_data_shows_no_data_screen(http, failure, reason):
    t = make_ticker(http.add(failure()))
    t.refresh(wait=True)
    assert t.assets == [] and not t.stale
    assert t.error_reason() == reason
    t.display.show = lambda img, hold=0: t.display.frames.append(img)  # no real waiting
    t.step()  # must not crash
    assert len(t.display.frames) == 1


def test_cold_start_offline_uses_cached_prices(http, ok):
    t = make_ticker(http.add(ok))
    t.refresh(wait=True)
    t.request_cache.flush(force=True)

    # new process, no network: shows the saved prices, marked stale
    http2 = type(http)().add(wifi_down())
    t2 = make_ticker(http2)
    t2.request_cache._mem = {k: (v[0] - 3600, v[1]) for k, v in t2.request_cache._mem.items()}  # an hour old
    t2.refresh(wait=True)
    assert [a["symbol"] for a in t2.assets] == ["BTC", "ETH", "ADA"]
    assert t2.stale and t2.stale_since is not None
    t2.step()
    assert all(has_stale_marker(f) for f in t2.display.frames)


def test_unexpected_exception_does_not_kill_ticker(http, ok):
    t = make_ticker(http.add(ok))
    t.refresh(wait=True)
    t.api.fetch = lambda: 1 / 0
    expire(t)
    t.refresh(wait=True)
    assert t.stale and t._fetching is None


def test_failed_fetch_from_old_settings_is_ignored(http, ok):
    t = make_ticker(http.add(wifi_down()))
    old_api = t.api
    t.api = object()  # settings changed while the fetch was running
    t._fetch(old_api)
    assert t.failures == 0 and t.last_error is None


def test_bad_settings_values_fall_back_to_defaults(monkeypatch):
    for key, value in {"REFRESH_RATE": "fast", "BRIGHTNESS": "70%", "SLEEP": "nan", "LAYOUT": "fancy",
                       "LED_SLOWDOWN_GPIO": "9", "DOWNLOAD_ICONS": "maybe", "WEATHER_LAT": "200"}.items():
        monkeypatch.setenv(key, value)
    c = Config.from_env()
    assert c.refresh_rate == 0 and c.brightness == 70 and c.sleep == 5 and c.layout == "classic"
    assert c.gpio_slowdown == 5 and c.download_icons is True and c.weather_lat == 90.0


def test_network_error_message_reason():
    t = Ticker.__new__(Ticker)
    t.last_error = NetworkError("x")
    assert t.error_reason() == "NO NETWORK?"
