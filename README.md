# Crypto Ticker 2.0

Crypto prices on an RGB LED matrix driven by a Raspberry Pi. This is an updated
version of the [Howchoo crypto ticker](https://howchoo.com/pi/raspberry-pi-cryptocurrency-ticker)
for current Raspberry Pi OS. The hardware is the same; the software is rewritten.

| `LAYOUT=classic` | `LAYOUT=chart` |
|--|--|
| ![classic](docs/preview.gif) | ![chart](docs/preview-chart.gif) |

Each coin screen shows the coin's icon, ticker, 24h change with ▲/▼, a 7-day chart and the
price (auto-sized: `$109,235`, `$4,012.37`, `$0.0₄1123`). Each cycle can also show a **clock**
with the weather, the crypto **Fear & Greed index**, and a **market indices** page. Screens slide
between each other. Everything can be changed from your phone.

More screenshots (rendered by the emulator) are in [`docs/screenshots/`](docs/screenshots/):

| Coin | Featured | List view (4) | List view (5) | Stale prices | No data |
|--|--|--|--|--|--|
| ![](docs/screenshots/07-coin-classic.png) | ![](docs/screenshots/06-featured.png) | ![](docs/screenshots/10d-list-view-4.png) | ![](docs/screenshots/10c-list-view-5.png) | ![](docs/screenshots/10-coin-stale.png) | ![](docs/screenshots/11-no-data-offline.png) |

## Contents

1. [Hardware](#hardware)
2. [Wiring check](#wiring-check)
3. [Install](#install)
4. [Configuration](#configuration)
5. [Phone settings page](#phone-settings-page)
6. [iPhone widget](#iphone-widget)
7. [Running without hardware (emulator)](#running-without-hardware-emulator)
8. [How failures are handled](#how-failures-are-handled)
9. [Troubleshooting](#troubleshooting)
10. [Development](#development)

## Hardware

- Raspberry Pi Zero W / WH (also works on Pi 3, 4 and 5)
- Adafruit 64×32 RGB LED matrix panel (HUB75). 64×64 and chained panels also work
- Adafruit RGB Matrix Bonnet (or HAT)
- 5 V power supply, **4 A or more**, with a 2.1 mm barrel plug
- microSD card, 8 GB or larger

## Wiring check

Do this with the power **unplugged**.

1. **Bonnet on the Pi.** Press the bonnet onto all 40 GPIO pins. Look from the side: no pins
   should show on either end, and the board should sit flat.
2. **Ribbon cable.** Plug the 16-pin ribbon from the bonnet into the panel's **INPUT** connector
   (the one the printed arrows point *away* from). The plugs are keyed; don't force them.
3. **Panel power.** Connect the panel's red/black power lead to the bonnet's screw terminal:
   **red to +5V, black to GND**. Double-check this; reversed polarity can damage the panel.
4. **Power supply.** The 5 V supply goes into the bonnet's barrel jack. The bonnet also powers
   the Pi through the header, so you don't need the Pi's own USB power.
   A Pi's USB port can't power the panel.
5. **Jumpers.** For a 64×32 panel, leave the bonnet's "E" address jumper alone (it's for 64×64
   panels). If you soldered the optional PWM jumper (GPIO4 to GPIO18), set
   `LED_GPIO_MAPPING=adafruit-hat-pwm`.

After installing (below), run the test pattern:

```bash
sudo systemctl stop crypto-ticker
sudo ./venv/bin/python ticker.py --test-pattern
```

It shows solid **red, green, blue, white**, then a white border with colored corners
(red top-left, green top-right, blue bottom-left, yellow bottom-right) and the panel size.

| What you see | Fix |
|--|--|
| Colors swapped (red shows as blue, etc.) | Set `LED_RGB_SEQUENCE` (e.g. `RBG`, `GRB`) |
| Only the top or bottom half lights, or rows repeat | Check the ribbon is in **INPUT**; check `LED_ROWS` / `LED_COLS` |
| Flicker, sparkles or garbled pixels | Raise `LED_SLOWDOWN_GPIO` by 1 |
| Nothing at all | Check the 5 V supply and the red/black power lead, then the log |
| Dim or brownouts on white | Use a stronger supply (4 A+), or lower `BRIGHTNESS` |

## Install

1. Flash **Raspberry Pi OS Lite** (Bookworm or newer) with Raspberry Pi Imager. Use the 32-bit
   version on a Pi Zero W/WH. In Imager's settings set a hostname, a username and password,
   Wi-Fi (a Pi Zero W only does **2.4 GHz**), your time zone, and enable SSH.
2. Boot the Pi and SSH in: `ssh <user>@<hostname>.local`
3. Install:

   ```bash
   sudo apt-get install -y git
   git clone https://github.com/DNA2RNA1/crypto-ticker.git
   cd crypto-ticker
   ./install.sh
   ```

   `install.sh` installs packages, creates a Python virtual environment, builds the LED library
   (**20–40 minutes on a Pi Zero**; leave it running), turns off onboard audio (it conflicts
   with the panel timing), caps the system journal at 16 MB, asks for a 4–8 digit PIN for the
   phone settings page, and installs a systemd service that starts the ticker at boot.
   It's safe to run again; it keeps your `settings.env`.

   Options: `--pin 1234` (no prompt), `--no-pin`, `--skip-build` (don't rebuild the LED
   library), `--no-service` (don't enable auto-start).
4. Pick your coins: `nano settings.env`
5. `sudo reboot`. The ticker starts on its own.

| Task | Command |
|--|--|
| Is it running? | `sudo systemctl status crypto-ticker` |
| Live log | `journalctl -u crypto-ticker -f` or `tail -f logs/ticker.log` |
| Apply hand edits to settings.env | `sudo systemctl restart crypto-ticker` |
| Update | `git pull && ./install.sh --skip-build && sudo systemctl restart crypto-ticker` |
| Stop / start at boot | `sudo systemctl disable --now crypto-ticker` / `sudo systemctl enable --now crypto-ticker` |

**First run tip:** start minimal (`SCREENS=coins`, `WEB_ENABLED=false`), get prices on the panel,
then turn features back on one at a time and watch the log after each.

## Configuration

All settings live in `settings.env` (created from `settings.env.example`, which documents every
option). Most can also be changed from the phone page. A typo doesn't stop the ticker: an invalid
value is logged as a warning and the default is used.

| Name | Default | What it does |
|--|--|--|
| `SYMBOLS` | starter list | Coins in order. `ada:cardano` pins an exact CoinGecko id |
| `CURRENCY` | `usd` | `usd`, `eur`, `gbp`, `cad`, `aud`, `jpy` |
| `COINGECKO_API_KEY` | | Optional free Demo key (steadier rate limits) |
| `REFRESH_RATE` | `auto` | `auto` (as fast as once a minute within the monthly budget) or seconds, min 60 |
| `MONTHLY_CALL_BUDGET` | `10000` | CoinGecko calls per month (free Demo plan: 10,000) |
| `SLEEP` | `5` | Seconds per coin screen |
| `TRANSITION` | `slide` | `slide` or `none` |
| `LAYOUT` | `classic` | `classic` (icon + mini chart), `chart` (full-screen chart), `mix` |
| `FEATURED` | | Starred coins: get a big-picture screen before their price |
| `FOCUS` | `off` | `on` = coin rotation shows only starred coins (clock, Fear & Greed and indices keep running). With nothing starred, all coins show |
| `LIST_VIEW` | `off` | `off`, `4` (pages of 4: price + % change; the phone page's switch turns this on) or `5` (pages of 5 coins: ▲/▼ + price). Starred coins keep their big screens |
| `LIST_PAGE_SECONDS` | `6` | Seconds per list page (2–60) |
| `SCREENS` | `clock,feargreed,indices,coins` | Screens at the start of each cycle (coins always last) |
| `INDICES_SOURCE` | `coingecko` | `coingecko` (tokenized funds, no extra calls) or `yahoo` (real index values; unofficial) |
| `TIMEZONE` | Pi's setting | Clock and night-dimming time zone, e.g. `Europe/London` |
| `WEATHER_PLACE` / `WEATHER_LAT` / `WEATHER_LON` / `TEMP_UNIT` | example values | Weather on the clock; easiest to set by ZIP code or city on the phone page |
| `BRIGHTNESS` | `70` | 1–100 |
| `DIM_HOURS` / `DIM_BRIGHTNESS` | `22-7` / `15` | Night dimming window (24 h clock) and its brightness |
| `DOWNLOAD_ICONS` | `true` | Fetch coin icons automatically |
| `WEB_ENABLED` / `WEB_PORT` / `WEB_PIN` | `true` / `8080` / asked at install | Phone settings page |
| `LED_ROWS` / `LED_COLS` / `LED_CHAIN` | `32` / `64` / `1` | Panel size and how many are chained |
| `LED_GPIO_MAPPING` | `adafruit-hat` | `adafruit-hat-pwm` if you soldered the PWM jumper |
| `LED_SLOWDOWN_GPIO` | `1` | Pi Zero 0–1, Pi 3 ≈2, Pi 4/5 ≈4. Raise it if the panel flickers |
| `LED_RGB_SEQUENCE` | `RGB` | Change if colors are swapped |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FILE` | `logs/ticker.log` | Rotating log file; blank = journal only |
| `LOG_MAX_KB` / `LOG_BACKUPS` | `512` / `3` | Log rotation: at most ~2 MB on the SD card |

**Custom icons:** tap a coin's picture on the phone page and choose a photo, or copy a PNG to
`icons/<symbol>.png`. Your pictures stay on the Pi (they're not committed to git).

## Phone settings page

1. On your phone (same Wi-Fi), open `http://<hostname>.local:8080`. `install.sh` prints the address.
2. Enter your PIN. Five wrong PINs lock the page for 5 minutes.
3. In Safari: **Share → Add to Home Screen** for an app icon.

You can search and add coins (with rank and icon, so you pick the real one), reorder, star (★),
turn on **Focus** and **List view**, change layout, brightness, time per coin, night dimming,
screens, weather location, currency and refresh rate. Changes show on the panel within seconds.

Every value is checked on the Pi before it's saved (ranges, allowed choices, coin-id format,
time zones, coordinates), and nothing is written unless all fields pass; the page shows which
field was wrong. Bad hand edits in `settings.env` don't break the page either.
The page is only reachable on your home network.

## iPhone widget

`iphone/CryptoTicker.js` is a widget for the free **Scriptable** app. It fetches its own data,
so it works away from home. Install Scriptable, tap **+**, paste the whole file and name it
"Crypto Ticker". Add a Scriptable widget, long-press → **Edit Widget**, choose the script and
set **Parameter**:

| Parameter | Shows |
|--|--|
| `coins:btc` | One coin (home screen, StandBy, lock screen) |
| `coins:btc,ada,snek` | Several coins: medium widget shows 3; lock-screen inline rotates one per refresh; rectangular shows 2 |
| `news:https://example.com/feed.xml` | Latest headline from an RSS or Atom feed; tap opens the article |
| `btc,ada` | Old form, still works (same as `coins:`) |

Lock-screen widgets: **inline** (one line above the clock), **circular** (symbol, ▲/▼ and a ring
gauge of the 24h change), **rectangular** (one coin with a sparkline, two coins, or a headline
with source and age). They're monochrome, so direction is always shown with ▲/▼. If a fetch fails,
the last good data is shown and marked "offline" (home screen) or ⚠︎ (lock screen). The comments
at the top of the script have more setup examples. iOS decides when widgets refresh
(usually every 15–30 minutes).

## Running without hardware (emulator)

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements-dev.txt
python ticker.py --emulate --sample        # made-up prices, no network needed
python ticker.py --emulate                 # live prices
```

Open **http://localhost:8888** to watch the emulated panel (and http://localhost:8080 for the
settings page). Other emulator outputs: `--emulator-adapter pygame` (desktop window),
`terminal`, `tkinter`. Emulator look (pixel size, style) is in `emulator_config.json`.

Screenshots of every screen:

```bash
python screenshots.py                      # writes docs/screenshots/*.png
```

Animated GIF preview: `python preview.py --sample` (writes `preview.gif`).

## How failures are handled

- **Timeouts, Wi-Fi loss, server errors (5xx):** each request is retried up to 3 times with
  exponential backoff (about 1 s, 2 s). If that fails, the ticker keeps showing the **last good
  prices with a small red dot in the bottom-right corner** (the "stale" marker) and tries again
  after 30 s, 1 min, 2 min, 4 min… up to 10 min. Fetching runs in the background, so the
  display never freezes.
- **Rate limit (HTTP 429):** not retried on the spot. The ticker waits at least as long as
  CoinGecko's `Retry-After` header asks (min 1 min, backing off up to 30 min).
- **Malformed JSON or odd data:** bad rows are skipped; if nothing usable came back it's treated
  as a failed refresh (stale marker), never a crash.
- **No data at all:** after a reboot without internet, the last good prices are loaded from
  `cache/requests.json` and shown as stale. With no cache either, a `NO DATA` screen says why
  (`NO NETWORK?`, `RATE LIMIT`, `BAD DATA`).
- **Request cache:** identical requests within 50 s (prices) or 1 h (coin search) are served
  from memory without spending API calls. The disk copy is written at most every 5 minutes to
  spare the SD card.
- **Logs:** console/journal plus a rotating file (`logs/ticker.log`, 512 KB × 4 files); the
  system journal is capped at 16 MB by `install.sh`.

## Troubleshooting

| Problem | What to do |
|--|--|
| Panel blank, service running | `journalctl -u crypto-ticker -n 50`. Did you reboot after install (audio must be off)? Run `--test-pattern` |
| Flicker / garbled pixels | Raise `LED_SLOWDOWN_GPIO` by 1, restart. Soldering the PWM jumper also helps |
| Service keeps restarting | `journalctl -u crypto-ticker -b` shows the error. `ModuleNotFoundError: rgbmatrix` → run `./install.sh` without `--skip-build` |
| `NO DATA / NO NETWORK?` | Pi isn't online: `ping -c3 api.coingecko.com`. Pi Zero W needs 2.4 GHz Wi-Fi |
| `NO DATA / RATE LIMIT` or the red stale dot often | Use `REFRESH_RATE=auto`, add a free CoinGecko Demo key. The phone page shows this month's call count |
| Red dot in the corner | Prices couldn't be refreshed; they're the last good ones. Check the log for why |
| Wrong coin for a ticker | Pin it: `SYMBOLS=btc,uni:uniswap` |
| Can't open the phone page | Same Wi-Fi? Try the Pi's IP (`hostname -I`). Check `WEB_ENABLED=true` and the log |
| Forgot the PIN | Edit `WEB_PIN` in `settings.env`, then `sudo systemctl restart crypto-ticker` |
| Wrong time on the clock | Set `TIMEZONE` (or the location on the phone page); check `timedatectl` |
| SD card filling up | `du -sh logs cache`; lower `LOG_MAX_KB` / `LOG_BACKUPS` |
| Focus on but every coin shows | No coin is starred; tap ★ on the phone page (the page shows a hint) |

## Development

```bash
pip install -r requirements-dev.txt
pytest                      # Python tests (mocked APIs, no network or hardware)
node --test tests/widget/widget.test.mjs   # iPhone widget tests (also run by pytest if Node is installed)
```

The tests mock CoinGecko responses: normal data, timeouts, HTTP 429, HTTP 5xx, malformed JSON,
Wi-Fi loss, cold start offline, plus settings-page validation, Focus/List view, log rotation and
the emulator. The widget tests run the Scriptable script against a small stand-in for
Scriptable's API (`tests/widget/scriptable_stub.mjs`), not the real app.

| File | Role |
|--|--|
| `ticker.py` | Main loop, config, background refresh with backoff, screens, Focus/List view |
| `prices.py` | CoinGecko client: retries, typed errors, tolerant parsing, cache |
| `cache.py` / `retry.py` / `logsetup.py` | Request cache, backoff helpers, rotating logs |
| `render.py` | Draws every screen as a Pillow image |
| `display.py` | Real panel, emulator (with screenshots) or GIF recorder |
| `webui.py` | Phone settings page with input validation |
| `feeds.py`, `session.py`, `usage.py`, `icons.py`, `bdf.py` | Weather/F&G/indices, market sessions, API budget, icons, fonts |
| `install.sh`, `crypto-ticker.service` | Install and auto-start |
| `screenshots.py`, `preview.py`, `sample.py` | Emulator screenshots, GIF preview, sample data |
| `iphone/CryptoTicker.js` | Scriptable widget |

Price data from [CoinGecko](https://www.coingecko.com/en/api). Icons from CoinGecko, with
[spothq/cryptocurrency-icons](https://github.com/spothq/cryptocurrency-icons) as a fallback.
LED driver: [hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix).
Emulator: [RGBMatrixEmulator](https://github.com/ty-porter/RGBMatrixEmulator).
