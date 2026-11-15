#!/usr/bin/env python3
"""Joystickul Vesta de pe Raspberry Pi (GPIO17) -> activare în backendul Vesta de pe laptop.

Ținut apăsat 5 secunde = o activare, trimisă ca `POST /api/v1/device/events` (contractul API,
la fel ca orice dispozitiv). Fiecare apăsare are un `event_id` unic; reîncercările îl păstrează,
deci backendul nu creează două sesiuni dacă un răspuns se pierde pe rețea.

Moduri:
  (implicit)  ascultă joystickul și trimite activările
  --watch     doar afișează apăsările (verificarea cablajului), nu trimite nimic
  --check     verifică o singură dată dacă backendul răspunde

Configurare (variabile de mediu, de obicei din /etc/vesta/joystick.env):
  VESTA_BACKEND_URL      ex.: http://Cristi_Desktop.local:8000
  VESTA_DEVICE_API_KEY   aceeași cheie ca backendul (antetul X-Device-Key); nu apare în loguri
  VESTA_GPIO_DEVICE_ID   implicit vesta-rpi-01
  VESTA_GPIO_PIN         implicit 17 (BCM; pinul fizic 11)
  VESTA_HOLD_SECONDS     implicit 5.0
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

DEFAULT_DEVICE_ID = "vesta-rpi-01"
DEBOUNCE_SECONDS = 0.08
# O apăsare care nu a putut fi livrată în acest interval e abandonată: o sesiune pornită cu
# minute întârziere ar fi mai derutantă decât o apăsare repetată.
MAX_EVENT_AGE_SECONDS = 120.0
RETRY_DELAYS = (1.0, 2.0, 3.0, 5.0)


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {message}", flush=True)


@dataclass(frozen=True)
class Activation:
    event_id: str
    occurred_at: datetime
    created_monotonic: float

    @classmethod
    def new(cls) -> Activation:
        return cls(f"gpio-{uuid.uuid4()}", datetime.now(UTC), time.monotonic())


class PermanentError(RuntimeError):
    """Configurare greșită (cheie, date): reîncercarea nu ajută."""


class TemporaryError(RuntimeError):
    """Rețea sau backend oprit: se reîncearcă."""


class Backend:
    def __init__(self, base_url: str, device_key: str | None, device_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.device_key = device_key
        self.device_id = device_id

    def health(self) -> bool:
        try:
            with urllib.request.urlopen(self.base_url + "/health", timeout=3) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def activate(self, activation: Activation) -> str:
        body = json.dumps(
            {
                "event_id": activation.event_id,
                "device_id": self.device_id,
                "event": "activate",
                "occurred_at": activation.occurred_at.isoformat(),
            }
        ).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.device_key:
            headers["X-Device-Key"] = self.device_key
        request = urllib.request.Request(
            self.base_url + "/api/v1/device/events", data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise PermanentError(
                    f"HTTP {exc.code}: cheia dispozitivului nu se potrivește cu backendul"
                ) from None
            if exc.code in (400, 404, 422):
                raise PermanentError(f"HTTP {exc.code}: cererea a fost refuzată") from None
            raise TemporaryError(f"HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise TemporaryError(str(getattr(exc, "reason", exc))) from None
        # Răspunsul conține și tokenul de control al sesiunii: nu se loghează.
        session = payload.get("session") or {}
        return f"{payload.get('outcome', '?')} (sesiune {str(session.get('id', '?'))[:8]}…)"


def deliver(
    activation: Activation,
    send: Callable[[Activation], str],
    *,
    stop: threading.Event,
    now: Callable[[], float] = time.monotonic,
    max_age: float = MAX_EVENT_AGE_SECONDS,
    delays: tuple[float, ...] = RETRY_DELAYS,
) -> str | None:
    """Trimite o activare până reușește, până expiră sau până la o eroare permanentă."""

    attempt = 0
    while not stop.is_set():
        try:
            return send(activation)
        except PermanentError as exc:
            log(f"Activarea NU a fost trimisă: {exc}")
            return None
        except TemporaryError as exc:
            age = now() - activation.created_monotonic
            if age >= max_age:
                log(f"Activarea a expirat după {age:.0f} s fără backend ({exc}); apasă din nou")
                return None
            delay = delays[min(attempt, len(delays) - 1)]
            log(f"Backend indisponibil ({exc}); reîncerc în {delay:.0f} s")
            attempt += 1
            stop.wait(delay)
    return None


class Joystick:
    """O apăsare continuă de `hold_seconds` = un singur eveniment, până la eliberare."""

    def __init__(self, pin: int, hold_seconds: float, on_hold: Callable[[], None]) -> None:
        from gpiozero import Button

        self._on_hold = on_hold
        self.button = Button(
            pin,
            pull_up=True,
            bounce_time=DEBOUNCE_SECONDS,
            hold_time=hold_seconds,
            hold_repeat=False,
        )
        # Dacă joystickul e deja apăsat la pornire, nu declanșăm până nu e eliberat.
        self._armed = not self.button.is_pressed
        self._pressed_at: float | None = None
        self.hold_seconds = hold_seconds
        self.button.when_pressed = self._pressed
        self.button.when_held = self._held
        self.button.when_released = self._released

    def _pressed(self) -> None:
        # Jurnalul arată și apăsările scurte: așa se vede imediat dacă problema e cablajul.
        self._pressed_at = time.monotonic()
        log(f"Joystick apăsat (ține {self.hold_seconds:.0f} s pentru activare)")

    def _held(self) -> None:
        if self._armed:
            self._armed = False
            self._on_hold()

    def _released(self) -> None:
        if self._pressed_at is not None:
            duration = time.monotonic() - self._pressed_at
            if duration < self.hold_seconds:
                log(f"Joystick eliberat după {duration:.1f} s: prea scurt, nicio activare")
        self._pressed_at = None
        self._armed = True

    def close(self) -> None:
        self.button.close()


def run_watch(pin: int, hold_seconds: float, stop: threading.Event) -> None:
    from gpiozero import Button

    button = Button(pin, pull_up=True, bounce_time=DEBOUNCE_SECONDS, hold_time=hold_seconds)
    pressed_at: list[float] = []
    button.when_pressed = lambda: (pressed_at.append(time.monotonic()), log("APĂSAT"))
    button.when_held = lambda: log(f"ȚINUT {hold_seconds:.0f} s -> aici s-ar trimite activarea")
    button.when_released = lambda: log(
        f"ELIBERAT după {time.monotonic() - pressed_at[-1]:.1f} s" if pressed_at else "ELIBERAT"
    )
    log(f"Urmăresc GPIO{pin}: acum este {'APĂSAT' if button.is_pressed else 'liber'}. Ctrl+C oprește.")
    stop.wait()
    button.close()


def run_forwarder(backend: Backend, pin: int, hold_seconds: float, stop: threading.Event) -> None:
    pending: queue.Queue[Activation] = queue.Queue(maxsize=4)

    def on_hold() -> None:
        activation = Activation.new()
        log(f"Joystick ținut {hold_seconds:.0f} s: trimit activarea")
        try:
            pending.put_nowait(activation)
        except queue.Full:
            log("Prea multe activări în așteptare; apăsarea a fost ignorată")

    joystick = Joystick(pin, hold_seconds, on_hold)
    reachable = backend.health()
    log(
        f"Gata. GPIO{pin}, ținere {hold_seconds:.0f} s, dispozitiv {backend.device_id}, "
        f"backend {backend.base_url} ({'răspunde' if reachable else 'NU răspunde încă'})"
    )
    try:
        while not stop.is_set():
            try:
                activation = pending.get(timeout=0.5)
            except queue.Empty:
                continue
            result = deliver(activation, backend.activate, stop=stop)
            if result:
                log(f"Backend: {result}")
    finally:
        joystick.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Joystick Vesta (GPIO17) -> backend Vesta")
    parser.add_argument("--watch", action="store_true", help="doar afișează apăsările")
    parser.add_argument("--check", action="store_true", help="verifică backendul și ieși")
    parser.add_argument("--backend", default=os.environ.get("VESTA_BACKEND_URL", ""))
    args = parser.parse_args(argv)

    pin = int(os.environ.get("VESTA_GPIO_PIN", "17"))
    hold_seconds = float(os.environ.get("VESTA_HOLD_SECONDS", "5.0"))
    device_id = os.environ.get("VESTA_GPIO_DEVICE_ID", DEFAULT_DEVICE_ID).strip()
    device_key = (os.environ.get("VESTA_DEVICE_API_KEY") or "").strip() or None

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    if args.watch:
        run_watch(pin, hold_seconds, stop)
        return 0
    if not args.backend:
        log("Lipsește VESTA_BACKEND_URL (sau --backend).")
        return 2
    backend = Backend(args.backend, device_key, device_id)
    if args.check:
        ok = backend.health()
        log(f"Backend {backend.base_url}: {'răspunde' if ok else 'NU răspunde'}")
        return 0 if ok else 1
    run_forwarder(backend, pin, hold_seconds, stop)
    log("Oprit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
