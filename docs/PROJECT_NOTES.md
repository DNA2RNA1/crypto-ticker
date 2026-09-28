# Crypto Ticker: project notes

Last updated: 2026-09-27

## What it is
A crypto price ticker for a Raspberry Pi driving a 64×32 RGB LED matrix. It's a rewrite of the
Howchoo crypto-ticker (2021) that stopped working. It also includes a phone settings page and an
iPhone widget.

- Repo: https://github.com/DNA2RNA1/crypto-ticker (public, branch `main`)
- Original project: https://howchoo.com/pi/raspberry-pi-cryptocurrency-ticker
  and github.com/Howchoo/crypto-ticker (the original git history is kept)

## Hardware (from the original build)
- Raspberry Pi Zero WH
- Adafruit 64×32 RGB LED matrix panel
- Adafruit RGB Matrix Bonnet (`LED_GPIO_MAPPING=adafruit-hat`)
- 3D-printed case, with an optional power button on the SCL/GND pins

## Status
- [x] Rewritten and tested in the cloud: mocked prices, LED emulator, GIF previews
- [x] Pushed to GitHub
- [ ] **First run on the real Pi.** Not done yet. Panel timing (`LED_SLOWDOWN_GPIO`) and the
      live CoinGecko data are the untested parts.
- [ ] Install the iPhone widget in Scriptable. It was tested against a stand-in for Scriptable,
      not on a real phone.

## Next session: first install on the Pi (learn as you go)
Go step by step and explain each command. Type the key commands yourself.
1. On a Windows PC: install Raspberry Pi Imager, flash **Raspberry Pi OS Lite (32-bit)** (the Pi
   Zero W needs 32-bit). In Imager settings: hostname, username/password, Wi-Fi (**2.4 GHz only**
   on a Pi Zero W), enable SSH, your time zone.
2. Boot the Pi; find it on the network; SSH in from the PC (`ssh <user>@<hostname>.local`).
3. `git clone https://github.com/DNA2RNA1/crypto-ticker.git && cd crypto-ticker && ./install.sh`
   (choose a PIN when asked). The Pi Zero build takes about 20–40 min.
4. **First run minimal:** in `settings.env` set `SCREENS=coins` and `WEB_ENABLED=false`, reboot, get
   prices on the panel. Tune `LED_SLOWDOWN_GPIO` if it flickers.
5. Then enable one at a time: phone page → clock/weather → Fear & Greed → indices. Check
   `journalctl -u crypto-ticker -f` after each.
6. Optional: upload your own picture for the featured coin from the phone page (tap its icon).

## Why the old version died
1. The Docker base image was Debian Buster (end of life), and the LED library removed the
   `make build-python` step.
2. The CoinGecko `/coins/list` symbol lookup matched copycat tokens with the same ticker.
3. There was no error handling: a failed fetch crashed the program, and the error screen used a
   missing font path.

## How it works now
| File | Role |
|--|--|
| `ticker.py` | Main loop, config (`settings.env`), refresh, transitions, dimming, live reload |
| `feeds.py` | Background feeds: Fear & Greed (alternative.me), weather (Open-Meteo), Yahoo indices/Dow |
| `session.py` | Rebuilds the real index's "today vs previous close" from tokenized funds' hourly history |
| `usage.py` | Monthly CoinGecko call counter and the auto refresh interval |
| `prices.py` | CoinGecko `/coins/markets`: one call per refresh (price, 24h change, 7-day sparkline, icon URL). Also `/search` for the settings page |
| `render.py` | Draws every screen as a Pillow image. Layouts `classic`, `chart` and `mix`, plus price formatting |
| `bdf.py` | Small BDF font renderer (Pillow's only covers 256 characters, which loses €, ▲▼ and subscript digits) |
| `icons.py` | Coin icon cache: override → cache → CoinGecko → spothq on GitHub → colored badge. Sharpened and brightened for LEDs |
| `display.py` | Output: real panel (hzeller rpi-rgb-led-matrix), the emulator, or a GIF recorder |
| `webui.py` | Phone settings page (Python standard library only). PIN, coin search, reorder, ★ featured, picture upload, layout, brightness, dimming |
| `preview.py` | Renders an animated GIF with no hardware (`--sample` for made-up prices) |
| `install.sh` + `crypto-ticker.service` | Native install (venv, builds the LED library, turns off onboard audio, systemd auto-start, asks for the PIN) |
| `iphone/CryptoTicker.js` | Scriptable widget: small (StandBy), medium (1–3 coins), lock screen |

Key design point: every screen is just a Pillow image, so new screen types (weather, clock and so
on) plug into the same rotation and transitions.

## Decisions made
- **No Docker.** A native venv plus systemd is simpler and has fewer layers to break on a Pi Zero.
- **CoinGecko only.** CoinMarketCap was dropped. Refresh defaults to **auto** (`usage.py`): it counts
  calls per month (`cache/usage.json`), learns how many hours a day the ticker runs, and refreshes
  as fast as once a minute while aiming for ≤90% of 10,000 calls/month (the free Demo plan limit).
  Simulated: 4 h/day → 1 min, 8 h/day → ~1.3 min, 16 h/day → ~3 min, 24/7 → ~5 min. Fixed rates
  (1–30 min) are still selectable on the phone page, which shows a usage meter. An optional free Demo key goes in `COINGECKO_API_KEY`.
- **Coins pinned to exact CoinGecko IDs** (`sym:id`) so a ticker can't pick up a copycat token.
- **Chart color = 7-day trend; ▲/▼ number = 24h change.** They can disagree when a coin is up on
  the week but down today. The "7D" tag marks the chart. Coloring the chart by the 24h change is
  still an option.
- **Prices:** ≥ $1000 no cents; $1–1000 two decimals; under $1 always 4 significant digits;
  tiny prices use subscript zeros (`$0.0₄1123` = 0.00001123, the CoinGecko/DEX Screener style).
  Precision wins over font size.
- **Chart layout:** a small coin icon is shown unless it would collide with the 24h change; the
  "7D" tag steps aside if the price needs the room.
- **Symbols use bold fonts** (6x13B / 7x13B).
- **Featured coins** (`FEATURED=`): a splash with the coin's picture (top-left), symbol,
  24h change and price shows before its regular price screen. A custom picture can be uploaded
  from the phone page and stays on the Pi only (`icons/*.png` is gitignored).
- **Phone page extras:** ★ toggles featured; tap a coin's picture to upload your own (resized on
  the phone, so HEIC photos work); "Reset picture" undoes it.
- **The PIN is never committed.** `install.sh` asks for it and stores it only in `settings.env`
  (gitignored, chmod 600). Five wrong tries locks the page for 5 minutes. Home Wi-Fi only.
- The repo is public so the Pi can `git clone` without a token.

## Extra screens (start of each cycle)
- **Clock:** big white time, AM/PM (no seconds), a weather icon top-right (sun, partly cloudy, cloudy, rain, storm, snow,
  fog; a silver moon showing the real phase on clear nights), and a bottom row with the outdoor
  temperature as "77°F" (green) and day/date (orange, shortens to fit). No humidity
  and no standalone moon (a full moon looked like a sun). The city name shows small under the time.
  Weather from Open-Meteo (free, no key, every 15 min). Location is set on the phone page by
  **ZIP code or city** (Open-Meteo geocoding), which saves `WEATHER_PLACE/LAT/LON` and also sets
  `TIMEZONE` from the place. °F/°C toggle (`TEMP_UNIT`). If weather fails,
  only the date shows.
- **Fear & Greed:** crypto index from alternative.me (free, no key; updates daily, fetched hourly).
  Speedometer gauge (arc lit up to the value, white needle), big number, mood label, and change vs
  yesterday (D) and a week ago (W).
  CoinGecko's website has its own in-house F&G (it can differ a lot: 47 vs 70 on 2026-09-27),
  but no API endpoint for it was found.
- **Indices, default source CoinGecko:** tokenized funds SPYX (`sp500-xstock`) and QQQX
  (`nasdaq-xstock`), added to the same `/coins/markets` call, so they cost **no extra calls**.
  They trade 24/7, so the rolling 24h change is NOT used. `session.py` computes the change vs the
  previous session's 4pm ET close while the market is open, and freezes the last session's change
  when it's closed. Accurate to about an hour's move; market holidays aren't known.
  No Dow token exists on CoinGecko, so the **Dow comes from Yahoo** (^DJI) in this mode; if Yahoo
  fails the page shows just S&P and Nasdaq. Charts show the current (or last) session.
- **Indices page flips** from % change to the absolute change halfway through its time on screen:
  CoinGecko rows show **$ per tokenized fund share** (e.g. +$3.24 on SPYX ≈ SPY), NOT index points;
  Yahoo rows (Dow, or all in Yahoo mode) show **index points**.
- **Indices, optional source Yahoo** (`INDICES_SOURCE=yahoo`): real index values incl. the Dow
  (^GSPC, ^DJI, ^IXIC), intraday chart. Unofficial and can throttle or break; failures just hide
  the screen.
- All toggles are on the phone page (Screens section).
- CoinGecko calls are per request, not per coin: up to 250 coins fit in one call.

## Starter coin list (settings.env.example)
BTC, ETH, ADA, LINK, HBAR, PEPE, DOGE, SNEK, SOL, NIGHT, pinned as:
`btc:bitcoin, eth:ethereum, ada:cardano, link:chainlink, hbar:hedera-hashgraph, pepe:pepe,
doge:dogecoin, snek:snek, sol:solana, night:midnight-3`

Gotcha: CoinGecko's `midnight` ID is an unrelated Polygon meme token. Midnight Network's NIGHT
(Cardano partner chain, launched Dec 2025) is **`midnight-3`**.

## Everyday commands (on the Pi)
- Install: `sudo apt-get install -y git && git clone https://github.com/DNA2RNA1/crypto-ticker.git && cd crypto-ticker && ./install.sh`, then `sudo reboot`
- Update: `cd crypto-ticker && git pull && sudo systemctl restart crypto-ticker`
- Logs: `journalctl -u crypto-ticker -f`
- Settings page: `http://raspberrypi.local:8080` (or the Pi's hostname)
- Flicker or garbled pixels: raise `LED_SLOWDOWN_GPIO` by 1 (Pi Zero 0–1, Pi 3 ≈2, Pi 4/5 ≈4)

## Limits to remember
- The cloud workspace can't reach CoinGecko, so previews there use sample prices.
- iOS decides when widgets refresh (roughly every 15–30 minutes). Scriptable can't do Live Activities.
- The settings page only works on home Wi-Fi (Tailscale would be needed for outside access).

## Ideas for later
1. Clock/date screen (no network needed)
2. Weather via Open-Meteo (free, no key): current temp, icon, high/low, 24h chart
1. ~~Clock/date screen~~ done
3. Scrolling news headlines from RSS (AP, Reuters, CoinDesk…); needs a scrolling-text mode
4. ~~Indices + Fear & Greed~~ done; still open: gold, VIX
5. Portfolio total (off by default, since the panel is in view)
6. Screen rotation rules (e.g. coins most of the time, clock/weather every few minutes; clock only at night), all switchable from the settings page
7. Windows desktop version (a borderless Python window reusing render.py)

Suggested order: get the current version running on the Pi first, then 1 → 2 → 6 → 3.
