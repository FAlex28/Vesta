#!/usr/bin/env bash
# Instalează serviciul vesta-joystick pe Raspberry Pi.
# Utilizare: ./install.sh <VESTA_BACKEND_URL>    (cheia dispozitivului vine pe stdin, nu în argumente)
# De obicei rulat de deploy.ps1 de pe laptop.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(whoami)"
ENV_FILE="/etc/vesta/joystick.env"
BACKEND_URL="${1:?lipsește adresa backendului, ex.: http://Cristi_Desktop.local:8000}"

DEVICE_KEY=""
if [ ! -t 0 ]; then
  IFS= read -r DEVICE_KEY || true
  DEVICE_KEY="${DEVICE_KEY%$'\r'}"
  DEVICE_KEY="${DEVICE_KEY#$'\xef\xbb\xbf'}"  # BOM adăugat de Windows PowerShell 5.1
fi

python3 -c "import gpiozero, lgpio" 2>/dev/null || sudo apt-get install -y python3-gpiozero python3-lgpio
sudo usermod -aG gpio "$USER_NAME" || true

sudo install -d -m 700 /etc/vesta
TMP="$(mktemp)"
chmod 600 "$TMP"
{
  printf 'VESTA_BACKEND_URL=%s\n' "$BACKEND_URL"
  printf 'VESTA_DEVICE_API_KEY=%s\n' "$DEVICE_KEY"
  printf 'VESTA_GPIO_DEVICE_ID=vesta-rpi-01\n'
  printf 'VESTA_GPIO_PIN=17\n'
  printf 'VESTA_HOLD_SECONDS=5.0\n'
} > "$TMP"
sudo install -m 600 -o root -g root "$TMP" "$ENV_FILE"
rm -f "$TMP"

sudo tee /etc/systemd/system/vesta-joystick.service >/dev/null <<EOF
[Unit]
Description=Vesta - joystick GPIO17 catre backendul Vesta
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
SupplementaryGroups=gpio
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/joystick_forwarder.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable vesta-joystick.service >/dev/null
sudo systemctl restart vesta-joystick.service
sleep 2
systemctl --no-pager --lines=6 status vesta-joystick.service || true
if [ -n "$DEVICE_KEY" ]; then KEY_STATE="setată"; else KEY_STATE="LIPSEȘTE"; fi
echo "Backend: $BACKEND_URL  (cheia dispozitivului: $KEY_STATE)"
echo "Jurnal live: journalctl -u vesta-joystick -f"
