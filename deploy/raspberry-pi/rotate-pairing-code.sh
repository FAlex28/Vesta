#!/usr/bin/env bash
# New device pairing code (e.g. the label was lost): every paired phone must scan the new QR.
# Emergency contacts and their notifications are kept.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$REPO/deploy/raspberry-pi"
OWNER="$(stat -c %U /var/lib/vesta)"

new="$(python3 -c 'import secrets; print("".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(10)))')"
sudo sed -i "s#^VESTA_DEVICE_PAIRING_CODE=.*#VESTA_DEVICE_PAIRING_CODE=$new#" /etc/vesta/vesta.env

sudo systemctl stop vesta
sudo -u "$OWNER" env PYTHONPATH="$REPO/backend" "$REPO/backend/.venv/bin/python" -c "
from pathlib import Path
from app.services.directory import DeviceDirectory
print('Telefoane deconectate:', DeviceDirectory(Path('/var/lib/vesta/directory.json')).revoke_all_phones())
"
sudo systemctl start vesta
bash "$HERE/show-pairing-qr.sh"
