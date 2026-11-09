#!/usr/bin/env bash
# Opens the TV dashboard full-screen. install.sh --tv runs this from the desktop's
# autostart, so it starts by itself when the Pi boots to its desktop.
# It waits for the ticker's web page, then restarts the browser if it ever closes.
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="$(grep -E '^WEB_PORT=[0-9]+$' "$HERE/settings.env" 2>/dev/null | cut -d= -f2)"
URL="http://localhost:${PORT:-8080}/tv"

BROWSER="$(command -v chromium || command -v chromium-browser)"
[ -n "$BROWSER" ] || { echo "Chromium isn't installed (sudo apt install chromium)" >&2; exit 1; }

# wait up to 2 minutes for the ticker service to answer
for _ in $(seq 60); do
    curl -fs -o /dev/null "$URL" && break
    sleep 2
done

while true; do
    "$BROWSER" --kiosk --noerrdialogs --disable-infobars --no-first-run \
        --disable-session-crashed-bubble --disable-features=Translate \
        --check-for-update-interval=31536000 --password-store=basic \
        --autoplay-policy=no-user-gesture-required --ozone-platform-hint=auto \
        --user-data-dir="$HOME/.config/crypto-ticker-tv" "$URL"
    sleep 5
done
