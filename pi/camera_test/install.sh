#!/usr/bin/env bash
# Instalează dependențele și serviciul systemd pentru serverul de test al camerelor.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(whoami)"
CAMERA_ENV="/etc/vesta/camera.env"

sudo apt-get update
sudo apt-get install -y python3-opencv python3-numpy v4l-utils usbutils
sudo apt-get install -y python3-picamera2 || echo "picamera2 indisponibil - se folosește doar webcam USB"
sudo usermod -aG video "$USER_NAME" || true
sudo install -d -m 700 /etc/vesta
if ! sudo test -f "$CAMERA_ENV"; then
  CAMERA_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
  printf 'VESTA_CAMERA_API_KEY=%s\nVESTA_CAMERA_BIND_HOST=0.0.0.0\n' "$CAMERA_KEY" | sudo tee "$CAMERA_ENV" >/dev/null
fi
if ! sudo grep -q '^VESTA_CAMERA_BIND_HOST=' "$CAMERA_ENV"; then
  printf 'VESTA_CAMERA_BIND_HOST=0.0.0.0\n' | sudo tee -a "$CAMERA_ENV" >/dev/null
fi
if ! sudo grep -q '^VESTA_CAMERA_API_KEY=.' "$CAMERA_ENV"; then
  CAMERA_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
  printf 'VESTA_CAMERA_API_KEY=%s\n' "$CAMERA_KEY" | sudo tee -a "$CAMERA_ENV" >/dev/null
fi
sudo chmod 600 "$CAMERA_ENV"

sudo tee /etc/systemd/system/vesta-camera-test.service >/dev/null <<EOF
[Unit]
Description=Vesta - server de test camera termica + camera normala
After=network-online.target

[Service]
User=$USER_NAME
EnvironmentFile=$CAMERA_ENV
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/camera_server.py --host \${VESTA_CAMERA_BIND_HOST} --port 8080
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now vesta-camera-test.service
sleep 2
systemctl --no-pager --lines=5 status vesta-camera-test.service || true
echo "Serverul camerelor este protejat cu o cheie păstrată în $CAMERA_ENV."
echo "Backendul Vesta trebuie să folosească VESTA_CAMERA_BASE_URL și aceeași VESTA_CAMERA_API_KEY."
