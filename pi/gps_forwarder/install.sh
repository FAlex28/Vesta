#!/usr/bin/env bash
# Instalează serviciul vesta-gps pe Raspberry Pi. Folosește aceeași configurare ca joystickul
# (/etc/vesta/joystick.env: adresa backendului și cheia dispozitivului).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(whoami)"
ENV_FILE="/etc/vesta/joystick.env"

if ! sudo test -f "$ENV_FILE"; then
  echo "Lipsește $ENV_FILE. Instalează întâi joystickul (pi/joystick_forwarder/deploy.ps1)." >&2
  exit 1
fi

# UART pe GPIO14/15: hardware pornit, consola serială oprită (altfel GPS-ul nu poate folosi portul).
if ! grep -q '^enable_uart=1' /boot/firmware/config.txt; then
  echo "enable_uart=1" | sudo tee -a /boot/firmware/config.txt >/dev/null
  echo "UART pornit în /boot/firmware/config.txt: Pi-ul trebuie repornit o dată."
fi
if grep -q 'console=serial0' /boot/firmware/cmdline.txt; then
  sudo sed -i 's/console=serial0,[0-9]* //' /boot/firmware/cmdline.txt
  echo "Consola serială oprită: Pi-ul trebuie repornit o dată."
fi
sudo usermod -aG dialout "$USER_NAME" || true

sudo tee /etc/systemd/system/vesta-gps.service >/dev/null <<EOF
[Unit]
Description=Vesta - GPS pe UART (GPIO14/15) catre backendul Vesta
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
SupplementaryGroups=dialout
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/gps_forwarder.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable vesta-gps.service >/dev/null
sudo systemctl restart vesta-gps.service
sleep 3
systemctl --no-pager --lines=6 status vesta-gps.service || true
echo "Jurnal live: journalctl -u vesta-gps -f"
