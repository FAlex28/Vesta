#!/usr/bin/env bash
# Deploy the authenticated camera server over an existing standalone camera installation.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(whoami)"
CAMERA_ENV="/etc/vesta/camera.env"
SERVICE_FILE="/etc/systemd/system/vesta-camera-test.service"

if [ -f "$DIR/camera_server.py.new" ]; then
  cp -a "$DIR/camera_server.py" "$DIR/camera_server.py.backup-$(date +%Y%m%d%H%M%S)"
  mv "$DIR/camera_server.py.new" "$DIR/camera_server.py"
fi

sudo -n install -d -m 700 /etc/vesta
if ! sudo -n test -f "$CAMERA_ENV"; then
  CAMERA_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
  printf 'VESTA_CAMERA_API_KEY=%s\nVESTA_CAMERA_BIND_HOST=0.0.0.0\n' "$CAMERA_KEY" | sudo -n tee "$CAMERA_ENV" >/dev/null
fi
if ! sudo -n grep -q '^VESTA_CAMERA_API_KEY=.' "$CAMERA_ENV"; then
  CAMERA_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
  printf 'VESTA_CAMERA_API_KEY=%s\n' "$CAMERA_KEY" | sudo -n tee -a "$CAMERA_ENV" >/dev/null
fi
if ! sudo -n grep -q '^VESTA_CAMERA_BIND_HOST=' "$CAMERA_ENV"; then
  printf 'VESTA_CAMERA_BIND_HOST=0.0.0.0\n' | sudo -n tee -a "$CAMERA_ENV" >/dev/null
fi
sudo -n chmod 600 "$CAMERA_ENV"

sudo -n tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Vesta camera feeds
After=network-online.target
Wants=network-online.target

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

sudo -n systemctl daemon-reload
sudo -n systemctl enable --now vesta-camera-test.service
sudo -n systemctl restart vesta-camera-test.service
sleep 2

sudo -n python3 - <<'PY'
import json
import urllib.error
import urllib.request
from pathlib import Path

settings = dict(
    line.strip().split("=", 1)
    for line in Path("/etc/vesta/camera.env").read_text().splitlines()
    if "=" in line and not line.lstrip().startswith("#")
)
key = settings["VESTA_CAMERA_API_KEY"]
base = "http://127.0.0.1:8080"

try:
    urllib.request.urlopen(f"{base}/api/status", timeout=3)
    raise SystemExit("Unauthenticated status unexpectedly succeeded")
except urllib.error.HTTPError as exc:
    if exc.code != 401:
        raise

request = urllib.request.Request(
    f"{base}/api/status", headers={"X-Vesta-Camera-Key": key}
)
with urllib.request.urlopen(request, timeout=3) as response:
    cameras = json.load(response)

for name, path in (("rgb", "rgb"), ("thermal", "thermal")):
    camera = cameras[name]
    if not camera["connected"]:
        raise SystemExit(f"{name} camera is disconnected")
    stream_request = urllib.request.Request(
        f"{base}/stream/{path}.mjpg", headers={"X-Vesta-Camera-Key": key}
    )
    with urllib.request.urlopen(stream_request, timeout=4) as response:
        frame = response.read(4096)
        if not response.headers.get("Content-Type", "").startswith("multipart/x-mixed-replace"):
            raise SystemExit(f"{name} stream has an unexpected content type")
        if b"\xff\xd8" not in frame:
            raise SystemExit(f"{name} stream did not deliver a JPEG frame")
    print(f"{name}: connected, {camera['fps']} fps, authenticated MJPEG frame received")
PY

echo "Camera service is active and protected; its key remains in $CAMERA_ENV."
