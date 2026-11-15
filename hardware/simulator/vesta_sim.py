"""Raspberry Pi + Arduino Nano virtuale, cu programele reale de pe dispozitive.

Pe Pi-ul real rulează `pi/joystick_forwarder` (GPIO17 -> POST /api/v1/device/events) și
`pi/gps_forwarder` (UART NMEA -> POST /api/v1/device/location). Nano-ul e legat prin USB la
`nano_bridge.py bridge`. Aici rulează exact aceleași funcții; se înlocuiește doar hardware-ul:

  joystick GPIO17  -> pin gpiozero MockFactory (aceeași logică de debounce și ținere 5 s)
  modul GPS UART   -> generator de propoziții NMEA valide (GGA + RMC, 1 Hz)
  Nano pe USB      -> virtual_nano.py pe socket://127.0.0.1:7701 (sketch-ul .ino, oglindit)

Backendul nu se schimbă: primește aceleași cereri, cu aceeași cheie și același device_id.

  python hardware/simulator/vesta_sim.py                 # backend http://127.0.0.1:8000
  python hardware/simulator/vesta_sim.py --backend http://192.168.1.20:8000

API de control (folosit de pagina /buton?pi, prin proxy-ul Vite /virtual-pi):
  GET  /state                    Pi, GPS, Nano (LED, LCD, buzzer), bridge
  POST /gpio17/press|release     apasă / eliberează joystickul
  POST /gpio17/hold?seconds=5.5  apăsare ținută (pentru scripturi)
  POST /gps/fix?on=0|1           modulul are / nu are fix
  POST /gps/module?connected=0|1 modulul trimite / nu trimite date
  POST /nano/usb?connected=0|1   scoate / pune cablul USB al Nano-ului
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "pi" / "joystick_forwarder"), str(ROOT / "pi" / "gps_forwarder")]

import gps_forwarder  # noqa: E402
import joystick_forwarder  # noqa: E402
from gpiozero import Device  # noqa: E402
from gpiozero.pins.mock import MockFactory  # noqa: E402
from virtual_nano import VirtualNano  # noqa: E402

BRIDGE = ROOT / "hardware" / "arduino_nano" / "bridge" / "nano_bridge.py"
START = (45.7537, 21.2257)  # Timișoara, Piața Victoriei (același punct ca în smoke test)
_print_lock = threading.Lock()


def say(tag: str, message: str) -> None:
    with _print_lock:
        print(f"{time.strftime('%H:%M:%S')}  [{tag:<6}] {message}", flush=True)


# --------------------------------------------------------------------------------- GPS


def nmea(body: str) -> str:
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f"${body}*{checksum:02X}"


def _lat(value: float) -> tuple[str, str]:
    d = int(abs(value))
    return f"{d:02d}{(abs(value) - d) * 60:07.4f}", "N" if value >= 0 else "S"


def _lon(value: float) -> tuple[str, str]:
    d = int(abs(value))
    return f"{d:03d}{(abs(value) - d) * 60:07.4f}", "E" if value >= 0 else "W"


class VirtualGps:
    """Un NEO-6M care merge încet pe un cerc mic în jurul Pieței Victoriei."""

    def __init__(self) -> None:
        self.fix = True
        self.connected = True
        self.satellites = 8
        self.hdop = 1.2
        self.position = START
        self._t0 = time.monotonic()

    def sentences(self) -> list[str]:
        now = datetime.now(UTC)
        hhmmss = now.strftime("%H%M%S") + ".00"
        ddmmyy = now.strftime("%d%m%y")
        angle = (time.monotonic() - self._t0) / 120.0  # o tură în ~12 minute, ~1.4 m/s
        self.position = (START[0] + 0.0009 * math.sin(angle), START[1] + 0.0013 * math.cos(angle))
        lat, ns = _lat(self.position[0])
        lon, ew = _lon(self.position[1])
        if not self.fix:
            return [nmea(f"GPGGA,{hhmmss},,,,,0,00,99.99,,,,,,"), nmea(f"GPRMC,{hhmmss},V,,,,,,,{ddmmyy},,,N")]
        return [
            nmea(f"GPGGA,{hhmmss},{lat},{ns},{lon},{ew},1,{self.satellites:02d},{self.hdop:.2f},91.0,M,36.0,M,,"),
            nmea(f"GPRMC,{hhmmss},A,{lat},{ns},{lon},{ew},1.2,45.0,{ddmmyy},,,A"),
        ]

    def read_lines(self, port, baud, state, stop):
        """Înlocuiește gps_forwarder.read_lines (UART): aceeași interfață, aceleași efecte."""
        while not stop.is_set():
            if not self.connected:
                stop.wait(0.5)
                yield None  # niciun octet de la modul
                continue
            for line in self.sentences():
                state.last_byte_at = time.monotonic()
                yield line
            stop.wait(1.0)


# ------------------------------------------------------------------------------ Pi


class VirtualPi:
    def __init__(self, backend_url: str, device_key: str | None, device_id: str, hold_s: float, nano_port: int):
        self.backend_url = backend_url
        self.device_key = device_key
        self.device_id = device_id
        self.hold_s = hold_s
        self.stop = threading.Event()
        self.gps = VirtualGps()
        self.pressed_since: float | None = None
        self.nano = VirtualNano(port=nano_port, on_change=self._nano_changed)
        self.nano_port = nano_port
        self.bridge: subprocess.Popen | None = None
        Device.pin_factory = MockFactory()
        self.pin = Device.pin_factory.pin(17)

    # -- joystick -------------------------------------------------------------------------
    def press(self) -> None:
        if self.pressed_since is None:
            self.pressed_since = time.monotonic()
            self.pin.drive_low()  # SW pe GND, pull-up intern: apăsat = LOW

    def release(self) -> None:
        if self.pressed_since is not None:
            self.pressed_since = None
            self.pin.drive_high()

    # -- pornire --------------------------------------------------------------------------
    def start(self) -> None:
        joystick_forwarder.log = lambda m: say("PI", f"joystick: {m}")
        gps_forwarder.log = lambda m: say("PI", f"gps: {m}")
        gps_forwarder.read_lines = self.gps.read_lines

        joy_backend = joystick_forwarder.Backend(self.backend_url, self.device_key, self.device_id)
        gps_backend = gps_forwarder.Backend(self.backend_url, self.device_key, self.device_id)
        threading.Thread(
            target=joystick_forwarder.run_forwarder,
            args=(joy_backend, 17, self.hold_s, self.stop),
            name="pi-joystick",
            daemon=True,
        ).start()
        threading.Thread(
            target=gps_forwarder.run,
            args=("/dev/serial0", 9600, gps_backend, self.stop),
            name="pi-gps",
            daemon=True,
        ).start()
        self.nano.start()
        threading.Thread(target=self._run_bridge, name="nano-bridge", daemon=True).start()

    def _run_bridge(self) -> None:
        """Bridge-ul real, cu linia de comandă de producție; doar portul e cel virtual."""
        cmd = [sys.executable, "-u", str(BRIDGE), "bridge", "--port", f"socket://127.0.0.1:{self.nano_port}",
               "--backend", self.backend_url, "--device-id", self.device_id, "--keep-running"]  # fmt: skip
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.bridge = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                       text=True, encoding="utf-8", errors="replace")  # fmt: skip
        assert self.bridge.stdout is not None
        for line in self.bridge.stdout:
            say("BRIDGE", line.rstrip().split("  ", 1)[-1])

    def _nano_changed(self, view: dict) -> None:
        usb = "" if view["serial_open"] else "  (port USB închis)"
        say("NANO", f"LED {view['led']} {view['led_pattern']} | LCD: {view['lcd'][0]} / {view['lcd'][1]} "
                    f"| buzzer: {view['buzzer']}{usb}")  # fmt: skip

    def shutdown(self) -> None:
        self.stop.set()
        if self.bridge and self.bridge.poll() is None:
            self.bridge.terminate()
        self.nano.stop()

    def state(self) -> dict:
        return {
            "pi": {
                "device_id": self.device_id,
                "backend": self.backend_url,
                "device_key": bool(self.device_key),
                "hold_seconds": self.hold_s,
                "gpio17": "pressed" if self.pressed_since is not None else "released",
                "held_for_s": round(time.monotonic() - self.pressed_since, 1) if self.pressed_since else 0,
            },
            "gps": {
                "connected": self.gps.connected,
                "fix": self.gps.fix,
                "latitude": round(self.gps.position[0], 6),
                "longitude": round(self.gps.position[1], 6),
                "satellites": self.gps.satellites if self.gps.fix else 0,
            },
            "nano": self.nano.view(),
            "bridge": {"running": bool(self.bridge and self.bridge.poll() is None)},
        }


# --------------------------------------------------------------------------- API HTTP


def make_handler(pi: VirtualPi):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # fără jurnal de acces în consolă
            pass

        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if urlparse(self.path).path in ("/state", "/"):
                self._reply(200, pi.state())
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            flag = lambda name: q.get(name, "1") not in ("0", "false", "off")  # noqa: E731
            path = url.path
            if path == "/gpio17/press":
                pi.press()
            elif path == "/gpio17/release":
                pi.release()
            elif path == "/gpio17/hold":
                seconds = float(q.get("seconds", pi.hold_s + 0.5))
                pi.press()
                threading.Timer(seconds, pi.release).start()
            elif path == "/gps/fix":
                pi.gps.fix = flag("on")
                say("PI", f"modul GPS: {'fix' if pi.gps.fix else 'fără fix'}")
            elif path == "/gps/module":
                pi.gps.connected = flag("connected")
                say("PI", f"modul GPS: {'conectat' if pi.gps.connected else 'deconectat (nu trimite date)'}")
            elif path == "/nano/usb":
                pi.nano.set_usb(flag("connected"))
                say("NANO", f"cablu USB {'conectat' if pi.nano.usb_connected else 'SCOS'}")
            else:
                self._reply(404, {"error": "not found"})
                return
            self._reply(200, pi.state())

    return Handler


def default_device_key() -> str | None:
    """Ca deploy.ps1: aceeași cheie pe care start-lan.ps1 o dă backendului."""
    key = (os.environ.get("VESTA_DEVICE_API_KEY") or "").strip()
    if key:
        return key
    path = ROOT / "backend" / ".state" / "device.key"
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Raspberry Pi + Arduino Nano virtuale pentru Vesta")
    parser.add_argument("--backend", default=os.environ.get("VESTA_BACKEND_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--device-id", default=os.environ.get("VESTA_GPIO_DEVICE_ID", "vesta-rpi-01"))
    parser.add_argument("--hold-seconds", type=float, default=float(os.environ.get("VESTA_HOLD_SECONDS", "5")))
    parser.add_argument("--api-port", type=int, default=8790)
    parser.add_argument("--nano-port", type=int, default=7701)
    args = parser.parse_args(argv)

    key = default_device_key()
    pi = VirtualPi(args.backend, key, args.device_id, args.hold_seconds, args.nano_port)
    server = ThreadingHTTPServer(("127.0.0.1", args.api_port), make_handler(pi))
    say("SIM", f"Raspberry Pi virtual: {args.device_id}, backend {args.backend}, "
               f"cheie dispozitiv {'da' if key else 'NU (backend fără VESTA_DEVICE_API_KEY)'}")  # fmt: skip
    say("SIM", f"Nano virtual pe socket://127.0.0.1:{args.nano_port}; API de control http://127.0.0.1:{args.api_port}")
    say("SIM", "Butonul: http://localhost:5173/buton?pi (ține 5 s). Ctrl+C oprește.")
    pi.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        say("SIM", "Opresc simulatorul")
        server.shutdown()
        pi.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
