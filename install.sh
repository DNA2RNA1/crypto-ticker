#!/usr/bin/env bash
# One-time setup on Raspberry Pi OS (Bookworm or newer, 32- or 64-bit).
# Run from the crypto-ticker folder:   ./install.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$HERE/../rpi-rgb-led-matrix"
VENV="$HERE/venv"

echo "==> Installing system packages"
sudo apt-get update
sudo apt-get install -y git python3-venv python3-dev cython3 cmake build-essential \
    libjpeg-dev zlib1g-dev libopenjp2-7 libtiff6

echo "==> Creating Python virtual environment"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel
"$VENV/bin/pip" install -r "$HERE/requirements.txt"

echo "==> Building the LED matrix library (takes a while on a Pi Zero)"
if [ ! -d "$LIB_DIR" ]; then
    git clone --depth 1 https://github.com/hzeller/rpi-rgb-led-matrix.git "$LIB_DIR"
else
    git -C "$LIB_DIR" pull --ff-only || true
fi
"$VENV/bin/pip" install "$LIB_DIR"

echo "==> Turning off onboard audio (it conflicts with the LED panel timing)"
echo "blacklist snd_bcm2835" | sudo tee /etc/modprobe.d/blacklist-rgb-matrix.conf >/dev/null
CONFIG_TXT=/boot/firmware/config.txt
[ -f "$CONFIG_TXT" ] || CONFIG_TXT=/boot/config.txt
if grep -q "^dtparam=audio=on" "$CONFIG_TXT"; then
    sudo sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$CONFIG_TXT"
fi

if [ ! -f "$HERE/settings.env" ]; then
    cp "$HERE/settings.env.example" "$HERE/settings.env"
    echo "==> Created settings.env - edit it to pick your coins"
fi

echo "==> Installing the auto-start service"
sed "s#__DIR__#$HERE#g" "$HERE/crypto-ticker.service" | sudo tee /etc/systemd/system/crypto-ticker.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable crypto-ticker.service

echo
echo "Done. Reboot once so the audio change takes effect:   sudo reboot"
echo "After that the ticker starts on its own at boot."
echo "  status:   sudo systemctl status crypto-ticker"
echo "  logs:     journalctl -u crypto-ticker -f"
echo "  restart:  sudo systemctl restart crypto-ticker   (after editing settings.env)"
