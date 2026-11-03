#!/usr/bin/env bash
# One-time setup on Raspberry Pi OS Lite (Bookworm or newer, 32- or 64-bit) with Python 3.
# Safe to run again: it updates what's there and keeps your settings.env.
#
#   ./install.sh                 normal install (asks for a settings-page PIN)
#   ./install.sh --pin 4826      set the PIN without asking
#   ./install.sh --no-pin        no PIN prompt (page is open to anyone on your Wi-Fi)
#   ./install.sh --skip-build    don't (re)build the LED library (it takes 20-40 min on a Pi Zero)
#   ./install.sh --no-service    install everything but don't enable auto-start
#   ./install.sh --deploy-key    only create the SSH deploy key for the Update button and show it
#   ./install.sh --enterprise-wifi         only add a work (WPA2-Enterprise, PEAP) Wi-Fi profile
#   ./install.sh --remove-enterprise-wifi  only remove that work Wi-Fi profile
#   ./install.sh --add-wifi      only save another ordinary Wi-Fi network (e.g. a phone hotspot)
#   ./install.sh --list-wifi     only list saved Wi-Fi networks and the ones in range
# A normal run never changes Wi-Fi settings; only the Wi-Fi options above do.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$(dirname "$HERE")/rpi-rgb-led-matrix"
VENV="$HERE/venv"
SERVICE=crypto-ticker
PIN="" ASK_PIN=1 BUILD=1 ENABLE_SERVICE=1 KEY_ONLY=0 WIFI=""
DEPLOY_KEY="$HOME/.ssh/crypto-ticker-deploy"

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
    case "$1" in
        --pin) PIN="${2:-}"; ASK_PIN=0; shift ;;
        --no-pin) ASK_PIN=0 ;;
        --skip-build) BUILD=0 ;;
        --no-service) ENABLE_SERVICE=0 ;;
        --deploy-key) KEY_ONLY=1 ;;
        --enterprise-wifi) WIFI=add ;;
        --remove-enterprise-wifi) WIFI=remove ;;
        --add-wifi) WIFI=add-psk ;;
        --list-wifi) WIFI=list ;;
        -h|--help) usage 0 ;;
        *) echo "Unknown option: $1" >&2; usage 1 ;;
    esac
    shift
done

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
valid_pin() { [[ "$1" =~ ^[0-9]{4,8}$ ]]; }

deploy_key() {  # SSH key the Update button uses to pull from GitHub (needed once the repo is private)
    if [ ! -f "$DEPLOY_KEY" ]; then
        mkdir -p "$HOME/.ssh"
        chmod 700 "$HOME/.ssh"
        ssh-keygen -q -t ed25519 -N "" -C "crypto-ticker@$(hostname)" -f "$DEPLOY_KEY"
    fi
    touch "$HOME/.ssh/known_hosts"
    if ! grep -q "^github.com " "$HOME/.ssh/known_hosts"; then
        ssh-keyscan -t ed25519 github.com 2>/dev/null >> "$HOME/.ssh/known_hosts" || true
    fi
    echo
    echo "Deploy key for the Update button (public half, safe to share):"
    echo
    cat "$DEPLOY_KEY.pub"
    echo
    echo "On GitHub: the repo -> Settings -> Deploy keys -> Add deploy key."
    echo "Paste the line above, leave \"Allow write access\" OFF, and save."
}

if [ "$KEY_ONLY" -eq 1 ]; then
    deploy_key
    exit 0
fi

# --- work Wi-Fi (only with --enterprise-wifi / --remove-enterprise-wifi) ---------
# Adds or removes one NetworkManager profile and exits; a normal install never gets here.
enterprise_wifi() {
    command -v nmcli >/dev/null || die "nmcli not found. Raspberry Pi OS Bookworm uses NetworkManager; older releases aren't supported."
    nmcli -t -f RUNNING general 2>/dev/null | grep -q running || die "NetworkManager isn't running."
    local helper="$HERE/wifi_enterprise.py"
    if [ "$WIFI" = remove ]; then
        sudo python3 "$helper" remove
        return
    fi
    if [ "$WIFI" = list ]; then
        python3 -B "$helper" list
        return
    fi
    if [ "$WIFI" = add-psk ]; then
        add_wifi "$helper"
        return
    fi
    if sudo python3 "$helper" exists; then
        read -rp "A work Wi-Fi profile already exists. Replace it? [y/N] " ans
        [[ "$ans" =~ ^[Yy] ]] || { echo "Left it as it is."; return; }
    fi
    local ssid user pass pass2 anon check="" ca="" domain="" choice confirm
    echo "Work Wi-Fi (WPA2-Enterprise, PEAP with MSCHAPv2). Your home Wi-Fi stays as it is."
    read -rp "Network name (SSID): " ssid
    read -rp "Username: " user
    while true; do
        read -rsp "Password: " pass; echo
        read -rsp "Password again: " pass2; echo
        [ -n "$pass" ] && [ "$pass" = "$pass2" ] && break
        echo "The passwords didn't match (or were empty). Try again."
    done
    unset pass2
    read -rp "Anonymous identity (outer identity; Enter to skip): " anon
    echo
    echo "How should the Pi check that it's talking to the real network?"
    echo "  1) a CA certificate file from your IT department (best)"
    echo "  2) the system's trusted certificates plus the server's domain name"
    echo "  3) skip the check (not recommended)"
    while [ -z "$check" ]; do
        read -rp "Choose 1, 2 or 3: " choice
        case "$choice" in
            1) check="file"
               read -rp "Path to the certificate (.pem/.crt): " ca
               ca="$(realpath -e "$ca" 2>/dev/null || true)"
               [ -n "$ca" ] || { echo "File not found."; check=""; continue; }
               read -rp "Server domain to match, e.g. radius.example.com (Enter to skip): " domain ;;
            2) check="system"
               read -rp "Server domain, e.g. radius.example.com: " domain
               [ -n "$domain" ] || { echo "The domain is needed for this option."; check=""; } ;;
            3) check="none"
               warn "Without a server check, any access point using this network name can collect your"
               warn "username and an MSCHAPv2 exchange that can be cracked offline to recover your password."
               read -rp "Type 'skip' to continue without the check: " confirm
               [ "$confirm" = skip ] || { echo "OK, choose again."; check=""; } ;;
            *) echo "Please type 1, 2 or 3." ;;
        esac
    done
    # Fields go to the helper on stdin, NUL-separated: never in a command line.
    printf '%s\0' "$ssid" "$user" "$pass" "$anon" "$check" "$ca" "$domain" | sudo python3 "$helper" add
    unset pass
    echo "NetworkManager will join it automatically when it's in range (your home network wins if both are)."
    echo "Remove it with:  ./install.sh --remove-enterprise-wifi"
}

add_wifi() {  # an ordinary WPA2/WPA3-Personal network, e.g. a phone hotspot
    local helper="$1" ssid pass pass2 hidden
    echo "Save another Wi-Fi network (for example a phone hotspot). Nothing else changes,"
    echo "and the Pi doesn't switch networks now: it joins this one when it's in range."
    read -rp "Network name (SSID): " ssid
    while true; do
        read -rsp "Password (Enter for an open network): " pass; echo
        [ -z "$pass" ] && break
        read -rsp "Password again: " pass2; echo
        [ "$pass" = "$pass2" ] && break
        echo "The passwords didn't match. Try again."
    done
    unset pass2
    read -rp "Is it a hidden network (doesn't show up in Wi-Fi lists)? [y/N] " hidden
    # name, password, hidden: on stdin, NUL-separated, never in a command line
    printf '%s\0' "$ssid" "$pass" "$hidden" | sudo python3 "$helper" add-psk
    unset pass
    echo "See all saved networks with:  ./install.sh --list-wifi"
}

if [ -n "$WIFI" ]; then
    enterprise_wifi
    exit 0
fi

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
sudo apt-get install -y git openssh-client python3 python3-venv python3-dev python3-pip cython3 cmake build-essential \
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
    # The library's Pillow shim includes Pillow's C header Imaging.h, which Pillow's
    # prebuilt wheels don't ship and the library's build doesn't locate (seen 2026-11:
    # "pillow.c: fatal error: Imaging.h"). Fetch the headers for the exact Pillow
    # version installed above and point the compiler at them.
    PIL_VER="$("$VENV/bin/python" -c 'import PIL; print(PIL.__version__)')"
    PIL_SRC="$(dirname "$HERE")/pillow-src-$PIL_VER"
    PIL_URL=https://github.com/python-pillow/Pillow.git
    if [ ! -f "$PIL_SRC/src/libImaging/Imaging.h" ]; then
        rm -rf "$PIL_SRC"
        # Only src/libImaging (about 2 MB) instead of the whole repository (over 100 MB);
        # a full shallow clone is the fallback if this git or network can't do a sparse one.
        if ! { git clone -q --depth 1 --filter=blob:none --sparse --branch "$PIL_VER" "$PIL_URL" "$PIL_SRC" \
               && git -C "$PIL_SRC" sparse-checkout set --no-cone /src/libImaging/; }; then
            rm -rf "$PIL_SRC"
            git clone -q --depth 1 --branch "$PIL_VER" "$PIL_URL" "$PIL_SRC" \
                || warn "could not fetch Pillow $PIL_VER headers; the LED library build may fail"
        fi
    fi
    # headers for other Pillow versions aren't needed any more
    for old in "$(dirname "$HERE")"/pillow-src-*; do
        if [ -d "$old" ] && [ "$old" != "$PIL_SRC" ]; then
            rm -rf "$old"
        fi
    done
    export CPATH="$PIL_SRC/src/libImaging${CPATH:+:$CPATH}"
    "$VENV/bin/pip" install "$LIB_DIR"
    # The LED library is compiled against this Pillow version's internals; doctor.sh
    # warns if Pillow changes later (the library then needs rebuilding: ./install.sh).
    echo "$PIL_VER" > "$VENV/.led-built-with-pillow"
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
say "Installing the launcher (the service runs this copy; see launcher.py)"
mkdir -p "$HERE/.launcher"
"$VENV/bin/python" "$HERE/launcher.py" --self-check
cp "$HERE/launcher.py" "$HERE/.launcher/launcher.py"

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

say "Setting up the deploy key for the Update button"
deploy_key

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
