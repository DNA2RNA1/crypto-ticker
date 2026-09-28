# Crypto Ticker 2.0

Crypto prices on an RGB LED matrix driven by a Raspberry Pi. This is an updated
version of the [Howchoo crypto ticker](https://howchoo.com/pi/raspberry-pi-cryptocurrency-ticker)
for current Raspberry Pi OS. The hardware is the same; the software is rewritten.

![preview](docs/preview.gif)

Each coin screen shows:

- the coin's icon (downloaded automatically and tuned to read well on LEDs)
- the ticker symbol and 24h change with a green ▲ or red ▼
- a 7-day sparkline (green if up over the week, red if down)
- the price, auto-sized to fit (`$109,235`, `$4,012.37`, `$0.0000123`)

Screens slide between coins. There's a loading screen, a `NO DATA` screen when
prices can't be fetched, and a small red dot in the corner if the prices shown
are stale. Optional night dimming.

## What changed from the original

| Problem in the old version | Fix |
|--|--|
| Docker image based on Debian Buster, which is end-of-life; the LED library also dropped the `make build-python` step the Dockerfile used | Runs natively in a Python virtual environment; `install.sh` builds the library the current way |
| CoinGecko symbol lookup matched every copycat token called "BTC" or "ETH" | Uses `/coins/markets?symbols=…`, which returns the top coin per ticker; `sym:coingecko-id` pins an exact coin |
| A failed price fetch crashed the program; the error screen pointed to a missing font | Retries, keeps showing the last prices, and shows a clear error screen |
| No icons, no transitions, no loading screen (all on the original author's wishlist) | Added |

The CoinMarketCap option was removed; CoinGecko covers everything with one API call per refresh.

## Hardware

- Raspberry Pi (Zero W/WH, 3, 4 or 5)
- 64×32 RGB LED matrix panel (64×64 and chained panels also work)
- Adafruit RGB Matrix Bonnet or HAT, and a 5V 4A power supply

## Install on the Pi

1. Flash **Raspberry Pi OS Lite** (Bookworm or newer) with Raspberry Pi Imager.
   Set Wi-Fi, a username, and enable SSH in the Imager settings.
2. SSH in and run:

   ```bash
   sudo apt-get install -y git
   git clone https://github.com/<your-account>/crypto-ticker.git
   cd crypto-ticker
   ./install.sh
   nano settings.env        # pick your coins
   sudo reboot
   ```

The ticker starts automatically at boot.

| Task | Command |
|--|--|
| Check it's running | `sudo systemctl status crypto-ticker` |
| Watch the log | `journalctl -u crypto-ticker -f` |
| Apply settings changes | `sudo systemctl restart crypto-ticker` |
| Update the code | `git pull && sudo systemctl restart crypto-ticker` |
| Stop it | `sudo systemctl stop crypto-ticker` |

## Settings

All settings live in `settings.env`; see `settings.env.example` for the full list with comments.

| Name | Default | What it does |
|--|--|--|
| SYMBOLS | btc,eth | Coins to show, in order. `ada:cardano` pins an exact CoinGecko id |
| CURRENCY | usd | Price currency (usd, eur, gbp, …) |
| COINGECKO_API_KEY | | Optional free Demo API key |
| REFRESH_RATE | 600 | Seconds between price refreshes (min 60) |
| SLEEP | 5 | Seconds each coin is shown |
| TRANSITION | slide | `slide` or `none` |
| BRIGHTNESS | 70 | 1–100 |
| DIM_HOURS / DIM_BRIGHTNESS | off / 15 | e.g. `22-7` dims from 10pm to 7am |
| DOWNLOAD_ICONS | true | Fetch coin icons automatically |
| LED_ROWS / LED_COLS / LED_CHAIN | 32 / 64 / 1 | Panel size |
| LED_GPIO_MAPPING | adafruit-hat | `adafruit-hat-pwm` if you soldered the PWM jumper |
| LED_SLOWDOWN_GPIO | 1 | Pi Zero 0–1, Pi 3 about 2, Pi 4/5 about 4. Raise it if the panel flickers |

**Custom icons:** put a PNG at `icons/<symbol>.png` (e.g. `icons/ada.png`) to replace the downloaded one.

## Preview without a Pi

```bash
pip install requests pillow
python preview.py --sample            # made-up prices, writes preview.gif
python preview.py                     # live prices
python preview.py --sample --rows 64  # preview a 64x64 panel
```

For a live on-screen emulator: `pip install RGBMatrixEmulator` then `python ticker.py --emulate`.

## Troubleshooting

- **Flicker or garbled pixels:** raise `LED_SLOWDOWN_GPIO` by 1 and restart.
- **`NO DATA / RATE LIMIT`:** raise `REFRESH_RATE`, or add a free CoinGecko Demo key.
- **Wrong coin for a ticker:** pin it, e.g. `SYMBOLS=btc,uni:uniswap`.
- **Blank panel, service running:** check the log; make sure you rebooted after install (the audio driver must be off).

Price data from [CoinGecko](https://www.coingecko.com/en/api). Icons from CoinGecko, with
[spothq/cryptocurrency-icons](https://github.com/spothq/cryptocurrency-icons) as a fallback.
LED driver: [hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix).
