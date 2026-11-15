#!/usr/bin/env python3
"""Puntea PC <-> Arduino Nano pentru Vesta.

Nano-ul (vesta_dispatcher.ino) primește numai comenzi seriale. Acest program rulează pe PC-ul
sau pe Raspberry Pi-ul la care e conectată placa prin USB, urmărește backendul Vesta și
traduce starea lui în protocolul plăcii.

Moduri:
  ports     listează porturile seriale găsite
  demo      rulează un scenariu fix pe placă (fără backend) ca să verifici LED-uri, buzzere, LCD
  manual    consolă: scrii comenzi de mână (STATE|ACTIVE, ...) și vezi răspunsurile plăcii
  bridge    urmărește backendul (WebSocket /ws/events + GET /api/v1/status) și comandă placa
  simulate  controlează o sesiune în backend fără Pi și fără telefon (activare, SOS, ...)

`--port sim` înlocuiește placa cu un simulator text, pentru teste fără hardware.

Starea afișată vine întotdeauna din GET /api/v1/status (sursa de adevăr). Evenimentele
WebSocket doar declanșează o recitire, conform contractului API: un client nu deduce starea
din istoricul local de evenimente.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

BAUD = 115200
PING_INTERVAL_S = 3.0  # Nano-ul declară legătura pierdută după 10 s fără comenzi.
STATUS_POLL_S = 5.0  # Recitire periodică, în plus față de evenimentele WebSocket.
BOOT_GRACE_S = 2.5  # Nano clasic se resetează la deschiderea portului (bootloader ~2 s).
# Backend repornit sau pornit după bridge: abia după atâtea secunde fără răspuns aprindem
# eroarea SERVER OFFLINE, ca placa să nu sune la fiecare restart scurt.
BACKEND_GRACE_S = 15.0
RESTART_DELAY_S = 3.0  # --keep-running: pauza înainte de repornirea bridge-ului după o eroare
NANO_ALERT_ID_MAX = 39  # char activeAlertId[40]
NANO_REASON_MAX = 23  # char alertReason[24] / technicalReason[24]
NANO_LINE_MAX = 118  # SERIAL_BUFFER_SIZE 120, fără '\n'

DEFAULT_BACKEND = "http://127.0.0.1:8000"
DEFAULT_DEVICE_ID = "vesta-rpi-01"

UNFINISHED_STATES = {"ACTIVE", "CHECK_IN", "UNCONFIRMED", "ALERT"}
SAFETY_TO_NANO = {"ACTIVE": "ACTIVE", "CHECK_IN": "CHECK", "UNCONFIRMED": "CHECK"}

# Problemele tehnice ale sesiunii afișate pe placă, în ordinea priorității. GPS-ul și pagina
# ascunsă nu sunt defecte ale lanțului de siguranță, deci nu aprind LED-ul albastru.
TECH_PRIORITY: tuple[str, ...] = (
    "network_disconnected",
    "microphone_denied",
    "audio_disconnected",
    "audio_interrupted",
    "voice_unavailable",
    "voice_timeout",
    "low_voltage",
    "power_unstable",
    "process_restarted",
)
# Nano traduce singur codurile cunoscute (NETWORK_DISCONNECTED -> CONEXIUNE etc.).
TECH_TO_NANO = {issue: issue.upper() for issue in TECH_PRIORITY}
TECH_TO_NANO["process_restarted"] = "REPORNIRE SERVER"
POWER_PROBLEMS = {"LOW_VOLTAGE", "POWER_UNSTABLE"}
BACKEND_OFFLINE = "SERVER OFFLINE"


# --------------------------------------------------------------------------------------
# Logica pură: status backend -> ce trebuie să afișeze placa -> comenzi seriale
# --------------------------------------------------------------------------------------


def sanitize(text: str, max_length: int) -> str:
    """Text sigur pentru protocolul serial: ASCII, fără separatorul '|' și fără newline."""

    cleaned = "".join(" " if ch in "|\r\n\t" else ch for ch in text)
    cleaned = cleaned.encode("ascii", errors="replace").decode("ascii")
    return cleaned.strip()[:max_length]


def wire_line(command: str) -> str:
    """O comandă completă pentru placă: păstrează '|', elimină newline-urile și non-ASCII."""

    cleaned = command.replace("\r", " ").replace("\n", " ")
    return cleaned.encode("ascii", errors="replace").decode("ascii").strip()[:NANO_LINE_MAX]


def nano_alert_id(alert_id: str) -> str:
    # Aceeași funcție la ALERT_RAISED și ALERT_RESOLVED, ca ID-urile să se potrivească pe placă.
    return sanitize(alert_id, NANO_ALERT_ID_MAX)


@dataclass(frozen=True)
class NanoView:
    """Ce afișează (sau trebuie să afișeze) placa."""

    safety: str = "STANDBY"  # STANDBY | ACTIVE | CHECK
    alert_id: str | None = None
    alert_reason: str | None = None
    technical: str | None = None


def _updated_at(session: dict[str, Any]) -> str:
    return str(session.get("updated_at") or "")


def derive_target(
    status: dict[str, Any] | None,
    *,
    device_id: str | None,
    backend_ok: bool,
    last_target: NanoView | None = None,
) -> tuple[NanoView, set[str]]:
    """Calculează starea dorită a plăcii și ID-urile alertelor închise explicit."""

    if not backend_ok or status is None:
        # Fără backend: păstrăm ultima stare cunoscută (o alertă nu se oprește singură)
        # și semnalăm separat problema tehnică.
        base = last_target or NanoView()
        return replace(base, technical=BACKEND_OFFLINE), set()

    sessions = [
        s
        for s in status.get("sessions") or []
        if device_id is None or s.get("device_id") == device_id
    ]
    resolved_ids = {
        s["alert"]["id"]
        for s in sessions
        if s.get("alert") and s["alert"].get("status") == "RESOLVED"
    }
    alerting = [s for s in sessions if s.get("alert") and s["alert"].get("status") == "ACTIVE"]
    unfinished = [s for s in sessions if s.get("state") in UNFINISHED_STATES]
    current = max(unfinished, key=_updated_at, default=None)
    alert_session = max(alerting, key=_updated_at, default=None)

    safety = SAFETY_TO_NANO.get(current["state"], "STANDBY") if current else "STANDBY"

    alert_id = alert_reason = None
    if alert_session is not None:
        alert_id = alert_session["alert"]["id"]
        alert_reason = alert_session.get("alert_reason") or alert_session["alert"].get(
            "incident_type"
        )
        if alert_reason == "EXPLICIT_SOS":
            alert_reason = "SOS_EXPLICIT"

    technical = None
    if current is not None:
        issues = set(current.get("active_technical_issues") or [])
        technical = next((TECH_TO_NANO[i] for i in TECH_PRIORITY if i in issues), None)
    if technical is None:
        power_state = (status.get("power") or {}).get("state")
        if power_state in POWER_PROBLEMS:
            technical = power_state

    return NanoView(safety, alert_id, alert_reason, technical), resolved_ids


def full_sync_commands(target: NanoView) -> tuple[list[str], NanoView]:
    """Comenzile după pornirea plăcii sau a bridge-ului: placa e în STANDBY, fără alertă."""

    # safety="UNKNOWN" forțează trimiterea STATE: placa poate să nu se fi resetat la deschiderea
    # portului și să afișeze încă o stare veche.
    commands, shown = plan_commands(NanoView(safety="UNKNOWN"), target, set())
    if target.technical is None:
        commands.append("TECHNICAL_RESOLVED")
    return ["HELLO", "READY", *commands], shown


def plan_commands(
    shown: NanoView,
    target: NanoView,
    resolved_ids: set[str],
) -> tuple[list[str], NanoView]:
    """Diferența dintre ce afișează placa și ce trebuie să afișeze, ca listă de comenzi.

    Reguli preluate din sketch:
    - cât timp o alertă e activă, placa refuză STATE, deci starea se trimite după închidere;
    - ALERT_RAISED și ALERT_RESOLVED pun placa în STANDBY;
    - o alertă se oprește numai când backendul o raportează RESOLVED. Dacă dispare fără
      închidere (de ex. backend repornit fără persistență), alarma continuă.
    """

    commands: list[str] = []
    new = shown

    if new.alert_id and new.alert_id != target.alert_id and new.alert_id in resolved_ids:
        commands.append(f"ALERT_RESOLVED|{nano_alert_id(new.alert_id)}")
        new = replace(new, alert_id=None, alert_reason=None, safety="STANDBY")

    if target.alert_id and target.alert_id != new.alert_id:
        reason = sanitize(target.alert_reason or "ALERTA", NANO_REASON_MAX)
        commands.append(f"ALERT_RAISED|{nano_alert_id(target.alert_id)}|{reason}")
        new = replace(
            new, alert_id=target.alert_id, alert_reason=target.alert_reason, safety="STANDBY"
        )

    if new.alert_id is None and target.safety != new.safety:
        commands.append(f"STATE|{target.safety}")
        new = replace(new, safety=target.safety)

    if target.technical != new.technical:
        if target.technical:
            commands.append(f"TECHNICAL_ERROR|{sanitize(target.technical, NANO_REASON_MAX)}")
        else:
            commands.append("TECHNICAL_RESOLVED")
        new = replace(new, technical=target.technical)

    return commands, new


# --------------------------------------------------------------------------------------
# Legătura serială (reală sau simulată)
# --------------------------------------------------------------------------------------

LinkCallback = Callable[[str, str], None]  # (tip, text): "line" | "opened" | "lost"


def _log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {message}", flush=True)


class SerialLink:
    """Port serial cu fir de citire și reconectare automată la deconectarea USB."""

    def __init__(self, port: str, baud: int = BAUD) -> None:
        self.port = port
        self.baud = baud
        self._ser: Any = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._callback: LinkCallback = lambda kind, text: None
        self._thread: threading.Thread | None = None

    @property
    def connected(self) -> bool:
        return self._ser is not None

    def start(self, callback: LinkCallback) -> None:
        self._callback = callback
        self._thread = threading.Thread(target=self._run, name="nano-serial", daemon=True)
        self._thread.start()

    def _open(self) -> bool:
        import serial  # pyserial

        try:
            ser = serial.Serial(self.port, self.baud, timeout=0.5, write_timeout=1)
        except (serial.SerialException, OSError) as exc:
            _log(f"Portul {self.port} nu se poate deschide: {exc}")
            return False
        with self._lock:
            self._ser = ser
        self._callback("opened", self.port)
        return True

    def _drop(self) -> None:
        with self._lock:
            ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
            self._callback("lost", self.port)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._ser is None and not self._open():
                self._stop.wait(2.0)
                continue
            try:
                raw = self._ser.readline()
            except Exception:
                self._drop()
                continue
            if raw:
                self._callback("line", raw.decode("ascii", errors="replace").strip())

    def send(self, command: str) -> bool:
        line = wire_line(command) + "\n"
        with self._lock:
            ser = self._ser
            if ser is None:
                return False
            try:
                ser.write(line.encode("ascii"))
                ser.flush()
                return True
            except Exception:
                pass
        self._drop()
        return False

    def close(self) -> None:
        self._stop.set()
        self._drop()


class SimulatedNano:
    """Imită răspunsurile și afișajul din vesta_dispatcher.ino, pentru teste fără placă."""

    ALERT_TEXT = {
        "SOS_EXPLICIT": "AJUTOR SOLICITAT",
        "MISSED_CHECK_INS": "FARA CONFIRMARE",
        "CHECKIN_TIMEOUT": "FARA CONFIRMARE",
    }
    TECH_TEXT = {
        "NETWORK_DISCONNECTED": "CONEXIUNE",
        "MICROPHONE_DENIED": "MICROFON",
        "AUDIO_INTERRUPTED": "AUDIO",
        "AUDIO_DISCONNECTED": "AUDIO",
        "VOICE_UNAVAILABLE": "SERVICIU VOCAL",
        "VOICE_TIMEOUT": "SERVICIU VOCAL",
        "LOW_VOLTAGE": "ALIMENTARE",
        "POWER_UNSTABLE": "ALIMENTARE",
    }

    def __init__(self, *, show_panel: bool = True) -> None:
        self.show_panel = show_panel
        self.safety = "STANDBY"
        self.alert_id = ""
        self.alert_text = ""
        self.technical = False
        self.technical_text = ""
        self.resolved_banner = False
        self.linked = False  # ca pcLinked din sketch: True după prima comandă, False după BYE
        self._callback: LinkCallback = lambda kind, text: None
        self._last_panel = ""
        self.started = time.monotonic()

    connected = True

    def start(self, callback: LinkCallback) -> None:
        self._callback = callback
        callback("opened", "sim")
        callback("line", "BOOT|VESTA_NANO|1")

    def close(self) -> None:
        pass

    def panel(self) -> str:
        if self.alert_id:
            return f"LED ROSU (clipeste) + buzzer D7 | LCD: ROSU 03 ALERTA / {self.alert_text[:16]}"
        if self.technical:
            text = self.technical_text or "EROARE TEHNICA"
            return f"LED ALBASTRU + buzzer D8 | LCD: ALBASTRU 04 ERR / {text[:16]}"
        if not self.linked and self.safety == "STANDBY":
            return "LED ALB (pauza scurta la 2 s) | LCD: ALB 00 STANDBY / Astept PC"
        if self.resolved_banner:
            return "LED ALB | LCD: ALB 00 STANDBY / Alerta rezolvata"
        return {
            "STANDBY": "LED ALB | LCD: ALB 00 STANDBY / Fara sesiune",
            "ACTIVE": "LED VERDE | LCD: VERDE 01 ACTIV / Conversatie OK",
            "CHECK": "LED GALBEN (clipeste) | LCD: GALBEN 02 CHECK / Astept raspuns",
        }[self.safety]

    def send(self, command: str) -> bool:
        reply = self.process(wire_line(command))
        if reply:
            self._callback("line", reply)
        panel = self.panel()
        if self.show_panel and panel != self._last_panel:
            self._last_panel = panel
            _log(f"[NANO SIM] {panel}")
        return True

    def process(self, line: str) -> str | None:  # noqa: PLR0911 - oglindește sketch-ul
        parts = line.split("|")
        command, args = parts[0], parts[1:]
        if not command:
            return None
        if command == "BYE":
            self.linked = False
            if not self.alert_id:
                self.safety = "STANDBY"
            self.technical = False
            self.technical_text = ""
            return "OK|BYE"
        self.linked = True
        if command == "HELLO":
            return "HELLO|VESTA_NANO|1"
        if command == "PING":
            return f"PONG|{int((time.monotonic() - self.started) * 1000)}"
        if command == "STATUS":
            return (
                f"STATUS|PC=1|STATE={'ALERT' if self.alert_id else self.safety}"
                f"|ALERT={int(bool(self.alert_id))}|TECH={int(self.technical)}|LCD=1|ID={self.alert_id}"
            )
        if command == "READY":
            self.technical = False
            return "OK|READY"
        if command == "STATE":
            if not args or not args[0]:
                return "ERR|MISSING_STATE"
            if self.alert_id:
                return "ERR|ALERT_ACTIVE"
            mapping = {
                "STANDBY": "STANDBY", "IDLE": "STANDBY", "CANCELLED": "STANDBY",
                "ACTIVE": "ACTIVE",
                "CHECK": "CHECK", "CHECK_IN": "CHECK", "UNCONFIRMED": "CHECK",
            }  # fmt: skip
            if args[0] not in mapping:
                return "ERR|UNKNOWN_STATE"
            self.safety = mapping[args[0]]
            self.resolved_banner = False
            return f"OK|STATE|{self.safety}"
        if command == "ALERT_RAISED":
            if not args or not args[0]:
                return "ERR|MISSING_ALERT_ID"
            self.alert_id = args[0][:NANO_ALERT_ID_MAX]
            reason = args[1] if len(args) > 1 else None
            self.alert_text = self.ALERT_TEXT.get(reason or "", reason or "ALERTA")
            self.safety = "STANDBY"
            self.resolved_banner = False
            return f"OK|ALERT_RAISED|{self.alert_id}"
        if command == "ALERT_RESOLVED":
            if not args or not args[0]:
                return "ERR|MISSING_ALERT_ID"
            if self.alert_id and args[0] != self.alert_id:
                return "ERR|ALERT_ID_MISMATCH"
            self.alert_id = ""
            self.safety = "STANDBY"
            self.resolved_banner = True
            return f"OK|ALERT_RESOLVED|{args[0]}"
        if command in {"TECHNICAL_ERROR", "DELIVERY_FAILED"}:
            self.technical = True
            reason = args[0] if args else None
            if command == "DELIVERY_FAILED":
                self.technical_text = "LIVRARE ESUATA"
            else:
                self.technical_text = self.TECH_TEXT.get(reason or "", reason or "EROARE TEHNICA")
            return f"OK|{command}"
        if command in {"TECHNICAL_RESOLVED", "DELIVERED"}:
            self.technical = False
            self.technical_text = ""
            return f"OK|{command}"
        if command == "CLEAR":
            if self.alert_id:
                return "ERR|ALERT_ACTIVE"
            self.technical = False
            self.technical_text = ""
            self.resolved_banner = False
            return "OK|CLEAR"
        return "ERR|UNKNOWN_COMMAND"


def say_goodbye(link: SerialLink | SimulatedNano) -> None:
    """Oprire voită: placa trece în „Astept PC”, fără eroare (o alertă activă continuă)."""

    if link.connected:
        link.send("BYE")
        time.sleep(0.1)
    link.close()


def resolve_port(requested: str | None) -> str:
    if requested:
        return requested
    from serial.tools import list_ports

    ports = list(list_ports.comports())
    markers = ("CH340", "CH341", "ARDUINO", "FTDI", "FT232", "USB-SERIAL", "USB SERIAL",
               "VID:PID=2341", "VID:PID=1A86", "VID:PID=0403")  # fmt: skip
    candidates = [
        p for p in ports if any(m in f"{p.description} {p.hwid}".upper() for m in markers)
    ]
    if len(candidates) == 1:
        _log(f"Placă găsită automat pe {candidates[0].device} ({candidates[0].description})")
        return candidates[0].device
    listing = "\n".join(f"  {p.device:<14} {p.description}" for p in ports) or "  (niciunul)"
    raise SystemExit(
        "Nu pot alege automat portul plăcii. Dă-l explicit cu --port (sau --port sim).\n"
        f"Porturi găsite:\n{listing}"
    )


def make_link(port: str | None, *, show_panel: bool = True) -> SerialLink | SimulatedNano:
    if port == "sim":
        return SimulatedNano(show_panel=show_panel)
    return SerialLink(resolve_port(port))


# --------------------------------------------------------------------------------------
# Client HTTP minimal pentru backend
# --------------------------------------------------------------------------------------


class BackendError(RuntimeError):
    pass


class Backend:
    def __init__(self, base_url: str, *, dashboard_key: str | None, device_key: str | None):
        self.base_url = base_url.rstrip("/")
        self.dashboard_key = dashboard_key
        self.device_key = device_key

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        all_headers = {"Accept": "application/json", **(headers or {})}
        if data is not None:
            all_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=all_headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                error = json.loads(detail)["error"]
                detail = f"{error['code']}: {error['message']}"
            except Exception:
                pass
            raise BackendError(f"{method} {path} -> HTTP {exc.code} {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BackendError(f"{method} {path} -> {exc}") from None

    def status(self) -> dict[str, Any]:
        headers = {"X-Dashboard-Key": self.dashboard_key} if self.dashboard_key else {}
        return self.request("GET", "/api/v1/status", headers=headers)

    @property
    def ws_url(self) -> str:
        if self.base_url.startswith("https://"):
            return "wss://" + self.base_url[len("https://") :] + "/ws/events"
        return "ws://" + self.base_url.removeprefix("http://") + "/ws/events"


# --------------------------------------------------------------------------------------
# Modul bridge
# --------------------------------------------------------------------------------------


class Bridge:
    def __init__(
        self,
        link: SerialLink | SimulatedNano,
        backend: Backend,
        *,
        device_id: str | None,
        verbose: bool,
    ) -> None:
        self.link = link
        self.backend = backend
        self.device_id = device_id
        self.verbose = verbose
        self.status: dict[str, Any] | None = None
        self.backend_ok: bool | None = None  # None = încă nu am încercat
        self.backend_down_since: float | None = None
        self.target: NanoView | None = None
        self.shown: NanoView | None = None  # None = placa trebuie resincronizată complet
        self.ready_at = 0.0
        self.last_ping = 0.0
        self._wake = asyncio.Event()
        self._status_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._warned_vanished: str | None = None

    # -- evenimente de pe placă (fir serial) --

    def _on_link(self, kind: str, text: str) -> None:
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._handle_link, kind, text)

    def _handle_link(self, kind: str, text: str) -> None:
        if kind == "opened":
            _log(f"Placă conectată pe {text}; aștept pornirea ei")
            self.shown = None
            self.ready_at = time.monotonic() + BOOT_GRACE_S
        elif kind == "lost":
            _log(f"Legătura cu placa pe {text} s-a pierdut; reîncerc")
            self.shown = None
        elif text.startswith("BOOT|"):
            _log(f"<- {text} (placa a pornit, resincronizez)")
            self.shown = None
            self.ready_at = time.monotonic()
        elif text.startswith("ERR|"):
            _log(f"<- {text}")
        elif self.verbose or not text.startswith(("PONG|", "OK|", "HELLO|")):
            _log(f"<- {text}")
        self._wake.set()

    # -- backend --

    async def refresh_status(self) -> None:
        async with self._status_lock:
            try:
                status = await asyncio.to_thread(self.backend.status)
            except BackendError as exc:
                if self.backend_ok is not False:
                    _log(f"Backend indisponibil: {exc}")
                self._backend_down()
            else:
                if not self.backend_ok:
                    _log(f"Backend conectat ({self.backend.base_url})")
                self.status = status
                self.backend_ok = True
                self.backend_down_since = None
        self._wake.set()

    async def watch_events(self) -> None:
        try:
            from websockets.asyncio.client import connect
        except ImportError:  # websockets < 13
            from websockets import connect  # type: ignore[no-redef]

        backoff = 1.0
        while True:
            try:
                async with connect(self.backend.ws_url, open_timeout=5) as ws:
                    if self.backend.dashboard_key:
                        await ws.send(
                            json.dumps({"type": "auth", "dashboard_key": self.backend.dashboard_key})
                        )
                    _log(f"WebSocket conectat: {self.backend.ws_url}")
                    backoff = 1.0
                    async for message in ws:
                        event = json.loads(message)
                        kind = event.get("type")
                        if kind == "system.heartbeat":
                            continue
                        if kind != "system.snapshot" and self.verbose:
                            _log(f"eveniment {kind}")
                        await self.refresh_status()
                    reason = getattr(ws, "close_reason", None) or ""
                    raise ConnectionError(f"închis de server {reason}".strip())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # rețea, autentificare, server oprit
                text = str(exc) or type(exc).__name__
                if "1008" in text or "Authentication" in text:
                    text += "  (verifică --dashboard-key / VESTA_DASHBOARD_API_KEY)"
                _log(f"WebSocket: {text}; reîncerc în {backoff:.0f} s")
                self._backend_down()
                self._wake.set()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    def _backend_down(self) -> None:
        if self.backend_down_since is None:
            self.backend_down_since = time.monotonic()
        self.backend_ok = False

    def backend_in_grace(self) -> bool:
        """Backendul tocmai a căzut (sau încă pornește): placa rămâne cum era, primește doar PING."""

        return (
            self.backend_ok is False
            and self.backend_down_since is not None
            and time.monotonic() - self.backend_down_since < BACKEND_GRACE_S
        )

    async def poll_status(self) -> None:
        while True:
            await self.refresh_status()
            await asyncio.sleep(STATUS_POLL_S)

    # -- sincronizarea plăcii --

    def _send(self, command: str) -> bool:
        if command != "PING" or self.verbose:
            _log(f"-> {command}")
        ok = self.link.send(command)
        if not ok:
            _log(f"   {command} NETRIMIS: placa nu e conectată")
        return ok

    def sync_once(self) -> None:
        if not self.link.connected or time.monotonic() < self.ready_at:
            return
        now = time.monotonic()
        if now - self.last_ping >= PING_INTERVAL_S:
            self.last_ping = now
            self._send("PING")
        if self.backend_ok is None or self.backend_in_grace():
            return  # PING-ul ține placa ocupată; starea vine după răspunsul backendului

        target, resolved = derive_target(
            self.status,
            device_id=self.device_id,
            backend_ok=self.backend_ok,
            last_target=self.target,
        )
        self.target = target

        if self.shown is None:
            commands, new_shown = full_sync_commands(target)
        else:
            commands, new_shown = plan_commands(self.shown, target, resolved)

        if (
            new_shown.alert_id
            and new_shown.alert_id != target.alert_id
            and self.backend_ok
            and self._warned_vanished != new_shown.alert_id
        ):
            self._warned_vanished = new_shown.alert_id
            _log(
                "ATENȚIE: alerta afișată nu mai apare în backend și nu a fost închisă explicit. "
                "Alarma rămâne pornită; o oprești din modul manual cu "
                f"ALERT_RESOLVED|{nano_alert_id(new_shown.alert_id)}"
            )

        for command in commands:
            if not self._send(command):
                self.shown = None
                return
        self.shown = new_shown

    async def sync_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.5)
            except TimeoutError:
                pass
            self._wake.clear()
            self.sync_once()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.link.start(self._on_link)
        _log(f"Bridge pornit. Backend: {self.backend.base_url}, dispozitiv: {self.device_id}")
        try:
            await asyncio.gather(self.sync_loop(), self.watch_events(), self.poll_status())
        finally:
            say_goodbye(self.link)


# --------------------------------------------------------------------------------------
# Modurile demo și manual
# --------------------------------------------------------------------------------------

DEMO_STEPS: tuple[tuple[tuple[str, ...], float, str], ...] = (
    (("HELLO", "READY", "STATE|STANDBY"), 4, "ALB fix; LCD 'ALB 00 STANDBY / Fara sesiune'"),
    (("STATE|ACTIVE",), 4, "VERDE fix; LCD 'VERDE 01 ACTIV / Conversatie OK'"),
    (("STATE|CHECK",), 5, "GALBEN clipește; LCD 'GALBEN 02 CHECK / Astept raspuns'"),
    (("STATE|ACTIVE",), 2, "înapoi la VERDE"),
    (
        ("ALERT_RAISED|demo-alerta-1|SOS_EXPLICIT",),
        6,
        "ROȘU clipește rapid + buzzer D7 (bip-bip-bip); LCD 'ROSU 03 ALERTA / AJUTOR SOLICITAT'",
    ),
    (
        ("ALERT_RESOLVED|alt-id", "STATE|ACTIVE"),
        3,
        "răspunde ERR|ALERT_ID_MISMATCH și ERR|ALERT_ACTIVE: alarma NU se oprește",
    ),
    (
        ("ALERT_RESOLVED|demo-alerta-1",),
        5,
        "două bipuri scurte pe D8; ALB, LCD 'Alerta rezolvata' ~5 s",
    ),
    (
        ("TECHNICAL_ERROR|MICROPHONE_DENIED",),
        6,
        "ALBASTRU clipește + D8 (biiiip lung x2); LCD 'ALBASTRU 04 ERR / MICROFON'",
    ),
    (
        ("ALERT_RAISED|demo-alerta-2|MISSED_CHECK_INS",),
        6,
        "alerta are prioritate: ROȘU + D7; LCD 'FARA CONFIRMARE'",
    ),
    (("ALERT_RESOLVED|demo-alerta-2",), 6, "după închidere reapare eroarea ALBASTRĂ (MICROFON)"),
    (("TECHNICAL_RESOLVED", "STATE|STANDBY"), 3, "ALB, liniște"),
)


def _print_link_event(kind: str, text: str) -> None:
    if kind == "line" and text.startswith("PONG|"):
        return
    _log(f"<- {text}" if kind == "line" else f"[{kind}] {text}")


def _wait_with_pings(link: SerialLink | SimulatedNano, seconds: float) -> None:
    end = time.monotonic() + seconds
    while (remaining := end - time.monotonic()) > 0:
        time.sleep(min(PING_INTERVAL_S, remaining))
        if time.monotonic() < end:
            link.send("PING")


def _wait_for_board(link: SerialLink | SimulatedNano) -> None:
    deadline = time.monotonic() + 10
    while not link.connected:
        if time.monotonic() > deadline:
            raise SystemExit("Placa nu s-a conectat în 10 s.")
        time.sleep(0.1)
    if isinstance(link, SerialLink):
        time.sleep(BOOT_GRACE_S)


def run_demo(link: SerialLink | SimulatedNano, *, include_timeout: bool) -> None:
    link.start(_print_link_event)
    _wait_for_board(link)
    _log("Demo: urmărește placa; fiecare pas spune ce ar trebui să vezi și să auzi.")
    for index, (commands, seconds, expected) in enumerate(DEMO_STEPS, start=1):
        print(f"\n[{index}/{len(DEMO_STEPS)}] {expected}", flush=True)
        for command in commands:
            _log(f"-> {command}")
            link.send(command)
            time.sleep(0.15)
        _wait_with_pings(link, seconds)
    if include_timeout:
        print("\n[timeout repaus] Nu mai trimit nimic 12 s, fără sesiune: placa NU dă eroare, "
              "arată ALB + 'Astept PC'.", flush=True)  # fmt: skip
        time.sleep(12)
        _log("-> PING (legătura revine: 'Fara sesiune')")
        link.send("PING")
        time.sleep(2)
        _log("-> STATE|ACTIVE")
        link.send("STATE|ACTIVE")
        print("\n[timeout în sesiune] Tac 12 s cu sesiunea activă: după 10 s placa arată "
              "ALBASTRU + 'CONEXIUNE PC' + D8 (informația ar putea fi veche).", flush=True)  # fmt: skip
        time.sleep(12)
        _log("-> PING + STATE|STANDBY (legătura revine)")
        link.send("PING")
        link.send("STATE|STANDBY")
        time.sleep(1)
    link.send("STATUS")
    time.sleep(0.5)
    _log("Demo terminat (BYE: placa rămâne în 'Astept PC', fără eroare).")
    say_goodbye(link)


def run_manual(link: SerialLink | SimulatedNano) -> None:
    pinging = threading.Event()
    pinging.set()

    def keepalive() -> None:
        while True:
            time.sleep(PING_INTERVAL_S)
            if pinging.is_set():
                link.send("PING")

    link.start(_print_link_event)
    _wait_for_board(link)
    threading.Thread(target=keepalive, daemon=True).start()
    print(
        "Consolă manuală. Exemple: STATE|ACTIVE  STATE|CHECK  ALERT_RAISED|a1|SOS_EXPLICIT\n"
        "  ALERT_RESOLVED|a1  TECHNICAL_ERROR|MICROPHONE_DENIED  TECHNICAL_RESOLVED  STATUS\n"
        "Comenzi locale: /ping off (testezi timeout-ul de 10 s), /ping on, /quit (trimite BYE)\n"
        "PING se trimite automat la 3 s.",
        flush=True,
    )
    try:
        while True:
            line = input("> ").strip()
            if not line:
                continue
            if line in {"/quit", "/q", "exit"}:
                break
            if line.startswith("/ping"):
                (pinging.clear if line.endswith("off") else pinging.set)()
                print(f"PING automat: {'pornit' if pinging.is_set() else 'oprit'}")
                continue
            link.send(line)
            time.sleep(0.2)
    except (EOFError, KeyboardInterrupt):
        pass
    say_goodbye(link)


# --------------------------------------------------------------------------------------
# Modul simulate: comandă backendul ca joystickul și telefonul
# --------------------------------------------------------------------------------------

SIM_HELP = """Comenzi:
  a | activate        ca joystickul ținut 5 s (POST /api/v1/device/events)
  st | status         starea sesiunii
  d | deliver         marchează verificarea curentă ca livrată (altfel nu duce la ALERT)
  ok                  „Sunt bine” pentru verificarea curentă
  s | sos             SOS explicit -> ALERT
  r | resolve         închide alerta activă (USER_CONFIRMED_SAFE)
  c | cancel          încheie sesiunea (USER_CANCELLED)
  t <problemă> [off]  problemă tehnică, ex.: t microphone_denied   /   t microphone_denied off
  w <secunde>         așteaptă (util în --script)
  q | quit"""


class Simulator:
    def __init__(self, backend: Backend, device_id: str) -> None:
        self.backend = backend
        self.device_id = device_id
        self.session_id: str | None = None
        self.token: str | None = None

    @staticmethod
    def _event_id(prefix: str) -> str:
        return f"sim-{prefix}-{uuid.uuid4().hex[:12]}"

    def _session_call(self, method: str, suffix: str = "", body: dict | None = None) -> dict:
        if not self.session_id or not self.token:
            raise BackendError("Nu există sesiune: rulează întâi „a” (activate).")
        return self.backend.request(
            method,
            f"/api/v1/sessions/{self.session_id}{suffix}",
            body,
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def show(self, session: dict[str, Any]) -> None:
        alert = session.get("alert") or {}
        print(
            f"  stare={session.get('state')}  sănătate={session.get('health_status')}  "
            f"verificare={session.get('checkin_id') or '-'} ({session.get('checkin_status') or '-'})  "
            f"alertă={alert.get('id', '-')} {alert.get('status', '')}  "
            f"probleme={','.join(session.get('active_technical_issues') or []) or '-'}"
        )

    def activate(self) -> None:
        headers = {"X-Device-Key": self.backend.device_key} if self.backend.device_key else {}
        result = self.backend.request(
            "POST",
            "/api/v1/device/events",
            {"event_id": self._event_id("act"), "device_id": self.device_id, "event": "activate"},
            headers=headers,
        )
        self.session_id = result["session"]["id"]
        self.token = result["control_token"]
        print(f"  {result['outcome']}  sesiune={self.session_id}")
        self.show(result["session"])

    def current(self) -> dict[str, Any]:
        return self._session_call("GET")

    def handle(self, words: list[str]) -> bool:
        command = words[0].lower()
        if command in {"q", "quit", "exit"}:
            return False
        if command in {"a", "activate"}:
            self.activate()
        elif command in {"st", "status"}:
            self.show(self.current())
        elif command in {"d", "deliver"}:
            checkin = self.current().get("checkin_id")
            if not checkin:
                raise BackendError("Nu există verificare activă (așteaptă CHECK_IN).")
            self.show(
                self._session_call(
                    "POST",
                    f"/checkins/{checkin}/delivery",
                    {"event_id": self._event_id("dlv"), "status": "DELIVERED", "channel": "android"},
                )["session"]
            )
        elif command == "ok":
            checkin = self.current().get("checkin_id")
            if not checkin:
                raise BackendError("Nu există verificare activă.")
            body = {"event_id": self._event_id("ok"), "checkin_id": checkin,
                    "confirmation": "safe", "source": "dashboard"}  # fmt: skip
            self.show(self._session_call("POST", "/checkins", body)["session"])
        elif command in {"s", "sos"}:
            body = {"event_id": self._event_id("sos"), "source": "dashboard",
                    "reason": "Test din simulator"}  # fmt: skip
            self.show(self._session_call("POST", "/sos", body)["session"])
        elif command in {"r", "resolve"}:
            alert = self.current().get("alert") or {}
            if alert.get("status") != "ACTIVE":
                raise BackendError("Nu există alertă activă.")
            body = {"event_id": self._event_id("res"), "action": "USER_CONFIRMED_SAFE"}
            self.show(self._session_call("POST", f"/alerts/{alert['id']}/resolve", body)["session"])
        elif command in {"c", "cancel"}:
            body = {"event_id": self._event_id("cnl"), "reason": "USER_CANCELLED"}
            self.show(self._session_call("POST", "/cancel", body)["session"])
        elif command in {"w", "wait"} and len(words) == 2:
            time.sleep(float(words[1]))
        elif command in {"t", "tech"} and len(words) >= 2:
            body = {"event_id": self._event_id("tec"), "issue": words[1].lower(),
                    "active": not (len(words) > 2 and words[2].lower() == "off"),
                    "source": "integration"}  # fmt: skip
            self.show(self._session_call("POST", "/technical-status", body)["session"])
        else:
            print(SIM_HELP)
        return True


def run_simulate(backend: Backend, device_id: str, script: Iterable[str] | None) -> None:
    sim = Simulator(backend, device_id)
    print(SIM_HELP, flush=True)
    lines = iter(script) if script is not None else None
    while True:
        try:
            line = next(lines) if lines is not None else input("sim> ")
        except (StopIteration, EOFError, KeyboardInterrupt):
            break
        words = line.split()
        if not words:
            continue
        if lines is not None:
            print(f"sim> {line}")
        try:
            if not sim.handle(words):
                break
        except BackendError as exc:
            print(f"  eroare: {exc}")


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Puntea PC <-> Arduino Nano pentru Vesta.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    def add_port(p: argparse.ArgumentParser) -> None:
        p.add_argument("--port", help="COM5, /dev/ttyUSB0 ... sau 'sim'. Implicit: detectare.")

    def add_backend(p: argparse.ArgumentParser) -> None:
        p.add_argument("--backend", default=os.environ.get("VESTA_BACKEND_URL", DEFAULT_BACKEND))
        p.add_argument("--device-id", default=os.environ.get("VESTA_GPIO_DEVICE_ID", DEFAULT_DEVICE_ID))

    sub.add_parser("ports", help="listează porturile seriale")

    demo = sub.add_parser("demo", help="scenariu fix pe placă, fără backend")
    add_port(demo)
    demo.add_argument("--with-timeout", action="store_true",
                      help="testează și pierderea legăturii (12 s fără PING)")  # fmt: skip

    manual = sub.add_parser("manual", help="consolă pentru comenzi manuale")
    add_port(manual)

    bridge = sub.add_parser("bridge", help="urmărește backendul și comandă placa")
    add_port(bridge)
    add_backend(bridge)
    bridge.add_argument("--dashboard-key", default=os.environ.get("VESTA_DASHBOARD_API_KEY"))
    bridge.add_argument("--all-devices", action="store_true",
                        help="nu filtra sesiunile după --device-id")  # fmt: skip
    bridge.add_argument("-v", "--verbose", action="store_true")
    bridge.add_argument("--keep-running", action="store_true",
                        help="repornește singur după o eroare neprevăzută (Ctrl+C oprește)")  # fmt: skip

    simulate = sub.add_parser("simulate", help="controlează o sesiune în backend")
    add_backend(simulate)
    simulate.add_argument("--device-key", default=os.environ.get("VESTA_DEVICE_API_KEY"))
    simulate.add_argument("--script", help="comenzi separate prin ';', ex.: \"a;st;s;r\"")
    return parser


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)

    if args.mode == "ports":
        from serial.tools import list_ports

        for p in list_ports.comports():
            print(f"{p.device:<14} {p.description}  [{p.hwid}]")
        return
    if args.mode == "demo":
        run_demo(make_link(args.port), include_timeout=args.with_timeout)
        return
    if args.mode == "manual":
        run_manual(make_link(args.port))
        return
    if args.mode == "simulate":
        backend = Backend(args.backend, dashboard_key=None, device_key=args.device_key)
        script = [s.strip() for s in args.script.split(";")] if args.script else None
        run_simulate(backend, args.device_id, script)
        return

    backend = Backend(args.backend, dashboard_key=args.dashboard_key, device_key=None)
    while True:
        bridge = Bridge(
            make_link(args.port),
            backend,
            device_id=None if args.all_devices else args.device_id,
            verbose=args.verbose,
        )
        try:
            asyncio.run(bridge.run())
            return
        except KeyboardInterrupt:
            _log("Oprit.")
            return
        except Exception as exc:  # noqa: BLE001 - --keep-running ține placa ocupată cu PING
            if not args.keep_running:
                raise
            _log(f"Bridge oprit de o eroare: {exc!r}; repornesc în {RESTART_DELAY_S:.0f} s")
            time.sleep(RESTART_DELAY_S)


if __name__ == "__main__":
    main()
