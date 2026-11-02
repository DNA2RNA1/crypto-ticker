#!/usr/bin/env bash
# One-time setup on Raspberry Pi OS Lite (Bookworm or newer, 32- or 64-bit) with Python 3.
# Safe to run again: it updates what's there and keeps your settings.env.
#
#   ./install.sh                 normal install (asks for a settings-page PIN)
#   ./install.sh --pin 4826      set the PIN without asking
#   ./install.sh --no-pin        no PIN prompt (page is open to anyone on your Wi-Fi)
#   ./install.sh --skip-build    don't (re)build the LED library (it takes 20-40 min on a Pi Zero)
#   ./install.sh --no-service    install everything but don't enable auto-start
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$(dirname "$HERE")/rpi-rgb-led-matrix"
VENV="$HERE/venv"
SERVICE=crypto-ticker
PIN="" ASK_PIN=1 BUILD=1 ENABLE_SERVICE=1

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
    case "$1" in
        --pin) PIN="${2:-}"; ASK_PIN=0; shift ;;
        --no-pin) ASK_PIN=0 ;;
        --skip-build) BUILD=0 ;;
        --no-service) ENABLE_SERVICE=0 ;;
        -h|--help) usage 0 ;;
        *) echo "Unknown option: $1" >&2; usage 1 ;;
    esac
    shift
done

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
valid_pin() { [[ "$1" =~ ^[0-9]{4,8}$ ]]; }

# --- checks ---------------------------------------------------------------
if [ "$(id -u)" -eq 0 ]; then
    warn "Run this as your normal user (it uses sudo where needed), not with sudo."
fi
command -v sudo >/dev/null || die "sudo is required."
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}:${ID_LIKE:-}" in
        *debian*|*raspbian*) ;;
        *) warn "This is ${PRETTY_NAME:-an unknown OS}; the script is written for Raspberry Pi OS." ;;
    esac
fi
MODEL="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)"
if [ -n "$MODEL" ]; then
    echo "Detected: $MODEL"
else
    warn "This doesn't look like a Raspberry Pi; the LED library may not work."
fi
if [ -n "$PIN" ] && ! valid_pin "$PIN"; then
    die "--pin must be 4 to 8 digits."
fi

# --- packages and Python --------------------------------------------------
say "Installing system packages"
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-dev python3-pip cython3 cmake build-essential \
    libjpeg-dev zlib1g-dev libopenjp2-7 libtiff6

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Python 3.9 or newer is needed (found $(python3 --version 2>&1))."

say "Creating the Python virtual environment"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel
"$VENV/bin/pip" install -r "$HERE/requirements.txt"

if [ "$BUILD" -eq 1 ]; then
    say "Building the LED matrix library (20-40 minutes on a Pi Zero; leave it running)"
    if [ ! -d "$LIB_DIR/.git" ]; then
        git clone --depth 1 https://github.com/hzeller/rpi-rgb-led-matrix.git "$LIB_DIR"
    else
        git -C "$LIB_DIR" pull --ff-only || warn "could not update $LIB_DIR; using the copy that's there"
    fi
    "$VENV/bin/pip" install "$LIB_DIR"
fi
"$VENV/bin/python" -c 'import rgbmatrix' 2>/dev/null \
    || warn "The rgbmatrix module isn't importable yet. Run again without --skip-build."

# --- Pi configuration -----------------------------------------------------
say "Turning off onboard audio (it uses the same hardware timer as the LED panel)"
echo "blacklist snd_bcm2835" | sudo tee /etc/modprobe.d/blacklist-rgb-matrix.conf >/dev/null
CONFIG_TXT=/boot/firmware/config.txt
[ -f "$CONFIG_TXT" ] || CONFIG_TXT=/boot/config.txt
if [ -f "$CONFIG_TXT" ] && grep -q "^dtparam=audio=on" "$CONFIG_TXT"; then
    sudo sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$CONFIG_TXT"
fi

say "Capping the system journal at 16 MB (small SD card)"
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=16M\nRuntimeMaxUse=16M\n' \
    | sudo tee /etc/systemd/journald.conf.d/crypto-ticker.conf >/dev/null
sudo systemctl restart systemd-journald || true

# --- settings -------------------------------------------------------------
mkdir -p "$HERE/logs" "$HERE/cache"
NEW_SETTINGS=0
if [ ! -f "$HERE/settings.env" ]; then
    cp "$HERE/settings.env.example" "$HERE/settings.env"
    NEW_SETTINGS=1
    say "Created settings.env from the example"
    case "$MODEL" in  # a sensible starting point for panel timing
        *"Pi 4"*|*"Pi 5"*) sed -i 's/^LED_SLOWDOWN_GPIO=.*/LED_SLOWDOWN_GPIO=4/' "$HERE/settings.env" ;;
        *"Pi 3"*|*"Zero 2"*)          sed -i 's/^LED_SLOWDOWN_GPIO=.*/LED_SLOWDOWN_GPIO=2/' "$HERE/settings.env" ;;
    esac
fi

set_setting() {  # set_setting KEY VALUE  (VALUE is already validated: digits only)
    if grep -q "^$1=" "$HERE/settings.env"; then
        sed -i "s/^$1=.*/$1=$2/" "$HERE/settings.env"
    else
        echo "$1=$2" >> "$HERE/settings.env"
    fi
}

if [ -n "$PIN" ]; then
    set_setting WEB_PIN "$PIN"
elif [ "$ASK_PIN" -eq 1 ] && ! grep -q "^WEB_PIN=." "$HERE/settings.env"; then
    while true; do
        read -rp "Choose a 4-8 digit PIN for the phone settings page (Enter for none): " PIN
        if [ -z "$PIN" ]; then
            warn "No PIN: anyone on your Wi-Fi can change the ticker's settings."
            break
        elif valid_pin "$PIN"; then
            set_setting WEB_PIN "$PIN"
            break
        fi
        echo "Please use 4 to 8 digits."
    done
fi
chmod 600 "$HERE/settings.env"

# --- service --------------------------------------------------------------
say "Installing the systemd service"
sed "s#__DIR__#$HERE#g" "$HERE/crypto-ticker.service" \
    | sudo tee /etc/systemd/system/$SERVICE.service >/dev/null
sudo systemctl daemon-reload
if [ "$ENABLE_SERVICE" -eq 1 ]; then
    sudo systemctl enable $SERVICE.service
fi
if command -v systemd-analyze >/dev/null; then
    sudo systemd-analyze verify /etc/systemd/system/$SERVICE.service || warn "systemd reported a problem with the unit file"
fi

PORT="$(grep -E '^WEB_PORT=[0-9]+$' "$HERE/settings.env" | cut -d= -f2 || true)"
cat <<EOF

Done.
$( [ "$NEW_SETTINGS" -eq 1 ] && echo "Edit your coins and panel settings first:  nano $HERE/settings.env" )
Reboot once so the audio change takes effect:   sudo reboot
After that the ticker starts on its own at boot.
  status:   sudo systemctl status $SERVICE
  logs:     journalctl -u $SERVICE -f     (or: tail -f $HERE/logs/ticker.log)
  restart:  sudo systemctl restart $SERVICE   (after editing settings.env by hand)

Phone settings page: http://$(hostname).local:${PORT:-8080}
  (in Safari: Share -> Add to Home Screen)
EOF
