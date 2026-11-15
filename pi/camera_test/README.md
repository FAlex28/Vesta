# Camera feeds for the Vesta dashboard

The Raspberry Pi camera service publishes the normal and thermal MJPEG feeds. Vesta's
backend proxies them under its authenticated dashboard API; browser clients never receive
the camera service key.

## Install on the Raspberry Pi

From the project root on the Pi, run:

```bash
bash pi/camera_test/install.sh
```

The installer creates `/etc/vesta/camera.env` with a random key and starts the camera
service on port `8080`. The normal camera and thermal camera must both be connected before
checking `systemctl status vesta-camera-test`.

The camera service requires `X-Vesta-Camera-Key` for its status and stream endpoints. The
key stays in `/etc/vesta/camera.env`; do not place it in a URL or frontend build variable.
The Vesta systemd unit reads that file too. When the backend runs on the same Pi, use:

```text
VESTA_CAMERA_BASE_URL=http://127.0.0.1:8080
```

## Backend on a laptop, cameras on the Pi

Set these variables only in the backend process environment:

```powershell
$env:VESTA_CAMERA_BASE_URL = "http://172.20.10.9:8080"
$env:VESTA_CAMERA_API_KEY = "<key from /etc/vesta/camera.env>"
```

Use the Pi's current hotspot IP if it changes. The dashboard connects to the backend,
which authenticates to the camera server and returns only connection state, frame size,
frame rate, and video. Thermal measurement endpoints are not part of the dashboard
contract.

## Verify

- `GET /api/v1/cameras/status` through the Vesta backend reports both cameras as connected.
- The dashboard should show both live feeds and their connection state.
- The Pi's direct `/api/status` and `/stream/...` endpoints should return `401` without
  `X-Vesta-Camera-Key`.
