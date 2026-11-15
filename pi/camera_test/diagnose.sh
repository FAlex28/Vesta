#!/usr/bin/env bash
# Arată ce vede Pi-ul pe USB și ce formate oferă fiecare cameră.
echo "== USB =="; lsusb
echo; echo "== Ultimele mesaje kernel (USB/UVC) =="; dmesg 2>/dev/null | grep -iE "usb|uvc|video" | tail -n 25 || sudo dmesg | grep -iE "usb|uvc|video" | tail -n 25
echo; echo "== V4L2 =="; v4l2-ctl --list-devices 2>&1
for d in /dev/video*; do
  name=$(cat /sys/class/video4linux/$(basename "$d")/name 2>/dev/null)
  case "$name" in *bcm2835*|*unicam*|*rpivid*|*pispbe*|*rp1-cfe*) continue;; esac
  echo; echo "--- $d ($name) ---"; v4l2-ctl -d "$d" --list-formats-ext 2>&1 | grep -E "\[|Size" | head -n 30
done
echo; echo "== libcamera =="; timeout 10 rpicam-hello --list-cameras 2>&1 | head -n 15 || echo "(rpicam-hello indisponibil sau blocat)"
echo; echo "== Detectare server =="; python3 "$(dirname "$0")/camera_server.py" --list
