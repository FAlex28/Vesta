#!/usr/bin/env bash
# Install Vesta on a Raspberry Pi 4 (Raspberry Pi OS Bookworm, Python 3.11).
# Run from the repository on the Pi:   bash deploy/raspberry-pi/install.sh
# Safe to run again: it keeps an existing /etc/vesta/vesta.env.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
USER_NAME="${SUDO_USER:-$(id -un)}"
HERE="$REPO/deploy/raspberry-pi"

echo "==> Vesta in $REPO, service user $USER_NAME"

echo "==> System packages (Python venv, GPIO via lgpio)"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip python3-lgpio python3-gpiozero qrencode

echo "==> Python environment"
# System site-packages give the venv the Pi's lgpio pin driver for gpiozero.
python3 -m venv --system-site-packages "$REPO/backend/.venv"
"$REPO/backend/.venv/bin/pip" install --upgrade pip
"$REPO/backend/.venv/bin/pip" install -e "$REPO/backend[raspberry]"

echo "==> GPIO permission"
sudo usermod -aG gpio "$USER_NAME"

echo "==> Configuration /etc/vesta/vesta.env"
sudo mkdir -p /etc/vesta
if [ ! -f /etc/vesta/vesta.env ]; then
  gen() { python3 -c 'import secrets; print(secrets.token_urlsafe(36))'; }
  device_code() { python3 -c 'import secrets; print("".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(10)))'; }
  sed -e "s#__REPO__#$REPO#g" \
      -e "s#^VESTA_CONTROL_SIGNING_SECRET=__GENERATED__#VESTA_CONTROL_SIGNING_SECRET=$(gen)#" \
      -e "s#^VESTA_DASHBOARD_API_KEY=__GENERATED__#VESTA_DASHBOARD_API_KEY=$(gen)#" \
      -e "s#^VESTA_DEVICE_API_KEY=__GENERATED__#VESTA_DEVICE_API_KEY=$(gen)#" \
      -e "s#^VESTA_DEVICE_PAIRING_CODE=__GENERATED_CODE__#VESTA_DEVICE_PAIRING_CODE=$(device_code)#" \
      "$HERE/vesta.env.example" | sudo tee /etc/vesta/vesta.env >/dev/null
  sudo chmod 600 /etc/vesta/vesta.env
  echo "    Created. Edit it for ElevenLabs / Tiger Data:  sudo nano /etc/vesta/vesta.env"
else
  echo "    Kept the existing file."
fi

echo "==> Web app"
if [ ! -f "$REPO/frontend/dist/index.html" ]; then
  echo "    frontend/dist is missing. On your laptop run:  cd frontend && npm ci && npm run build"
  echo "    then copy it:  scp -r frontend/dist $USER_NAME@<pi-address>:$REPO/frontend/"
fi

echo "==> systemd service"
sed -e "s#__REPO__#$REPO#g" -e "s#__USER__#$USER_NAME#g" "$HERE/vesta.service" \
  | sudo tee /etc/systemd/system/vesta.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable vesta.service
sudo systemctl restart vesta.service

sleep 3
if curl -fsS http://127.0.0.1:8000/health; then
  echo
  echo "==> Vesta is running. Dashboard key (keep it private):"
  sudo grep '^VESTA_DASHBOARD_API_KEY=' /etc/vesta/vesta.env | cut -d= -f2-
  echo "    Open http://$(hostname -I | awk '{print $1}'):8000/ on the laptop."
else
  echo "Vesta did not answer yet. Check:  journalctl -u vesta -n 50 --no-pager"
fi
echo "    If this was the first install, log out and in once so '$USER_NAME' joins the gpio group."
echo
echo "==> Next: set VESTA_PUBLIC_URL (stable https address), restart, then print the QR label:"
echo "    sudo nano /etc/vesta/vesta.env && sudo systemctl restart vesta"
echo "    bash $HERE/show-pairing-qr.sh"
