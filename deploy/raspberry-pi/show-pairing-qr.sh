#!/usr/bin/env bash
# Shows the device pairing QR code (to print as a label on the Vesta device) and saves a PNG.
# The phone scans it once: it opens <VESTA_PUBLIC_URL>/companion#pair=<code>.
set -euo pipefail
ENV_FILE=/etc/vesta/vesta.env
code="$(sudo grep -E '^VESTA_DEVICE_PAIRING_CODE=' "$ENV_FILE" | cut -d= -f2-)"
url="$(sudo grep -E '^VESTA_PUBLIC_URL=' "$ENV_FILE" | cut -d= -f2- | sed 's#/*$##')"
if [ -z "$code" ]; then echo "VESTA_DEVICE_PAIRING_CODE is empty in $ENV_FILE"; exit 1; fi
if [ -z "$url" ]; then
  echo "VESTA_PUBLIC_URL is empty. Set the stable https address in $ENV_FILE first (README step 8)."
  exit 1
fi
link="$url/companion#pair=$code"
qrencode -t ansiutf8 "$link"
qrencode -o "$HOME/vesta-pairing-qr.png" -s 12 -m 2 "$link"
echo
echo "Link:  $link"
echo "Cod (tipărit sub QR):  ${code:0:5}-${code:5}"
echo "PNG pentru etichetă:   $HOME/vesta-pairing-qr.png"
