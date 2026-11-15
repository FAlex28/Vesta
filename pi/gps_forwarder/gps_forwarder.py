#!/usr/bin/env python3
"""GPS-ul dispozitivului Vesta (UART pe GPIO14/15) -> locația live în backendul Vesta.

Modulul GPS (de ex. NEO-6M) trimite propoziții NMEA pe /dev/serial0, 9600 baud. Programul
citește RMC/GGA, verifică suma de control și, cât timp există un fix valid, trimite poziția la
`POST /api/v1/device/location` la fiecare 2 secunde (antet X-Device-Key). Backendul o atașează
sesiunii care rulează și o arată dispeceratului și persoanei de încredere doar în timpul unei
alerte; fără sesiune păstrează doar ultimul fix, ca o activare nouă să aibă locație din prima
secundă.

Moduri:
  (implicit)  citește GPS-ul și trimite pozițiile
  --watch     afișează ce primește de la modul (verificarea cablajului), nu trimite nimic

Configurare (de obicei /etc/vesta/joystick.env, aceeași cu a joystickului):
  VESTA_BACKEND_URL, VESTA_DEVICE_API_KEY, VESTA_GPIO_DEVICE_ID (implicit vesta-rpi-01)
  VESTA_GPS_PORT (implicit /dev/serial0), VESTA_GPS_BAUD (implicit 9600)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as dtime

SEND_EVERY_S = 2.0
NO_DATA_WARNING_S = 10.0
# Eroarea orizontală aproximată din HDOP pentru un modul GPS obișnuit (estimare, nu măsurătoare).
METERS_PER_HDOP = 5.0


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {message}", flush=True)


# ------------------------------------------------------------------------------- NMEA


def checksum_ok(sentence: str) -> bool:
    if not sentence.startswith("$") or "*" not in sentence:
        return False
    body, _, given = sentence[1:].partition("*")
    value = 0
    for char in body:
        value ^= ord(char)
    try:
        return value == int(given[:2], 16)
    except ValueError:
        return False


def _coordinate(value: str, hemisphere: str) -> float | None:
    if not value or hemisphere not in {"N", "S", "E", "W"}:
        return None
    try:
        degrees_len = 2 if hemisphere in {"N", "S"} else 3
        degrees = int(value[:degrees_len])
        minutes = float(value[degrees_len:])
    except ValueError:
        return None
    result = degrees + minutes / 60.0
    return -result if hemisphere in {"S", "W"} else result


@dataclass
class GpsState:
    latitude: float | None = None
    longitude: float | None = None
    fix_time: datetime | None = None
    valid: bool = False
    satellites: int | None = None
    hdop: float | None = None
    sentences: int = 0
    last_byte_at: float | None = None

    @property
    def accuracy_meters(self) -> float | None:
        return round(self.hdop * METERS_PER_HDOP, 1) if self.hdop is not None else None


def parse_sentence(sentence: str, state: GpsState) -> bool:
    """Updates `state`; True when a new position was read from an RMC sentence."""

    sentence = sentence.strip()
    if not checksum_ok(sentence):
        return False
    state.sentences += 1
    fields = sentence[1:].split("*")[0].split(",")
    kind = fields[0][2:]  # GPRMC / GNRMC -> RMC
    if kind == "GGA" and len(fields) >= 9:
        try:
            state.satellites = int(fields[7]) if fields[7] else None
            state.hdop = float(fields[8]) if fields[8] else None
        except ValueError:
            pass
        return False
    if kind == "RMC" and len(fields) >= 10:
        state.valid = fields[2] == "A"
        if not state.valid:
            return False
        latitude = _coordinate(fields[3], fields[4])
        longitude = _coordinate(fields[5], fields[6])
        if latitude is None or longitude is None:
            state.valid = False
            return False
        try:
            hhmmss, ddmmyy = fields[1], fields[9]
            moment = dtime(int(hhmmss[0:2]), int(hhmmss[2:4]), int(float(hhmmss[4:])), tzinfo=UTC)
            day = date(2000 + int(ddmmyy[4:6]), int(ddmmyy[2:4]), int(ddmmyy[0:2]))
            state.fix_time = datetime.combine(day, moment)
        except (ValueError, IndexError):
            state.fix_time = datetime.now(UTC)
        state.latitude, state.longitude = latitude, longitude
        return True
    return False


# ------------------------------------------------------------------------------- serial


def open_serial(port: str, baud: int) -> int:
    import termios

    fd = os.open(port, os.O_RDONLY | os.O_NOCTTY)
    attrs = termios.tcgetattr(fd)
    speed = getattr(termios, f"B{baud}")
    attrs[0] = 0  # iflag: raw
    attrs[1] = 0  # oflag
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
    attrs[3] = 0  # lflag: no echo, no canonical
    attrs[4] = attrs[5] = speed
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 5  # read returns after 0.5 s without data
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


def read_lines(port: str, baud: int, state: GpsState, stop: threading.Event):
    fd = open_serial(port, baud)
    buffer = b""
    try:
        while not stop.is_set():
            chunk = os.read(fd, 256)
            if not chunk:
                yield None
                continue
            state.last_byte_at = time.monotonic()
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield line.decode("ascii", errors="replace").strip()
            if len(buffer) > 4096:
                buffer = b""
    finally:
        os.close(fd)


# ------------------------------------------------------------------------------- backend


class Backend:
    def __init__(self, base_url: str, device_key: str | None, device_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.device_key = device_key
        self.device_id = device_id

    def send(self, state: GpsState) -> str:
        body = {
            "event_id": f"gps-{uuid.uuid4()}",
            "device_id": self.device_id,
            "latitude": round(state.latitude, 7),
            "longitude": round(state.longitude, 7),
            "captured_at": (state.fix_time or datetime.now(UTC)).isoformat(),
            "accuracy_meters": state.accuracy_meters,
            "satellites": state.satellites,
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.device_key:
            headers["X-Device-Key"] = self.device_key
        request = urllib.request.Request(
            self.base_url + "/api/v1/device/location",
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            answer = json.loads(response.read() or b"{}")
        return "sesiune activă" if answer.get("session_id") else "fără sesiune (păstrat ca ultim fix)"


# ------------------------------------------------------------------------------- main loop


def run(port: str, baud: int, backend: Backend | None, stop: threading.Event) -> None:
    state = GpsState()
    started = time.monotonic()
    last_sent = 0.0
    last_report = 0.0
    last_send_result = ""
    warned_no_data = False
    log(f"Citesc GPS-ul pe {port} la {baud} baud" + ("" if backend else " (doar afișare)"))
    for line in read_lines(port, baud, state, stop):
        now = time.monotonic()
        if line is not None:
            if backend is None and line.startswith("$"):
                print(f"   {line}", flush=True)
            new_fix = parse_sentence(line, state)
            if new_fix and backend is not None and now - last_sent >= SEND_EVERY_S:
                last_sent = now
                try:
                    result = backend.send(state)
                    if result != last_send_result:
                        log(f"Poziție trimisă ({state.latitude:.5f}, {state.longitude:.5f}, "
                            f"{state.satellites or '?'} sateliți): {result}")  # fmt: skip
                        last_send_result = result
                except urllib.error.HTTPError as exc:
                    log(f"Backendul a refuzat poziția: HTTP {exc.code}"
                        + (" (cheia dispozitivului diferă)" if exc.code in (401, 403) else ""))  # fmt: skip
                    last_send_result = ""
                except (urllib.error.URLError, OSError, TimeoutError) as exc:
                    log(f"Backend indisponibil ({getattr(exc, 'reason', exc)}); reîncerc")
                    last_send_result = ""
        silent_for = now - (state.last_byte_at or started)
        if silent_for > NO_DATA_WARNING_S and not warned_no_data:
            warned_no_data = True
            log(
                "Nicio dată de la modulul GPS. Verifică: TX al GPS-ului pe pinul 10 (GPIO15/RXD), "
                "RX al GPS-ului pe pinul 8 (GPIO14/TXD), VCC pe pinul 1 (3,3 V) sau 2 (5 V, după modul), "
                "GND pe pinul 9."
            )
        if state.last_byte_at and silent_for < 1:
            warned_no_data = False
        if now - last_report > 30:
            last_report = now
            if state.sentences and not state.valid:
                log(f"GPS-ul răspunde, dar nu are încă fix (sateliți: {state.satellites or 0}). "
                    "Afară sau lângă fereastră durează 1–5 minute la prima pornire.")  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GPS Vesta (UART) -> backend Vesta")
    parser.add_argument("--watch", action="store_true", help="doar afișează propozițiile NMEA")
    parser.add_argument("--port", default=os.environ.get("VESTA_GPS_PORT", "/dev/serial0"))
    parser.add_argument("--baud", type=int, default=int(os.environ.get("VESTA_GPS_BAUD", "9600")))
    parser.add_argument("--backend", default=os.environ.get("VESTA_BACKEND_URL", ""))
    args = parser.parse_args(argv)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    backend = None
    if not args.watch:
        if not args.backend:
            log("Lipsește VESTA_BACKEND_URL (sau --backend).")
            return 2
        backend = Backend(
            args.backend,
            (os.environ.get("VESTA_DEVICE_API_KEY") or "").strip() or None,
            os.environ.get("VESTA_GPIO_DEVICE_ID", "vesta-rpi-01").strip(),
        )
    try:
        run(args.port, args.baud, backend, stop)
    except OSError as exc:
        log(f"Portul {args.port} nu se poate deschide: {exc}. Activează UART: sudo raspi-config → "
            "Interface Options → Serial Port: login shell NU, hardware DA.")  # fmt: skip
        return 1
    log("Oprit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
