"""Arduino Nano virtual: oglindește vesta_dispatcher.ino, linie cu linie.

Portul USB serial e înlocuit de un socket TCP (implicit 127.0.0.1:7701). Bridge-ul real se
conectează exact ca la placă: `nano_bridge.py bridge --port socket://127.0.0.1:7701`.

Ce e identic cu sketch-ul: comenzile și răspunsurile (inclusiv trunchierile buffer-elor C),
expirarea legăturii după 10 s fără comenzi (LINK|LOST), prioritatea LED-urilor, textele LCD,
modelele buzzerelor și resetarea plăcii la deschiderea portului (BOOT după bootloader).
Ce lipsește: hardware-ul (LCD-ul e mereu prezent, nu există magistrală I2C blocată).
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field

# --- constante din vesta_dispatcher.ino --------------------------------------------------
PC_TIMEOUT_MS = 10_000
RESOLVED_DISPLAY_MS = 5_000
SERIAL_BUFFER_SIZE = 120  # char serialBuffer[120] -> maxim 119 caractere pe linie
ALERT_ID_SIZE = 40  # char activeAlertId[40]
REASON_SIZE = 24  # char alertReason[24], technicalReason[24]
BOOTLOADER_S = 1.5  # Nano clasic: bootloader-ul rulează ~1-2 s după reset; octeții se pierd

STANDBY, ACTIVE, CHECK = 0, 1, 2
LED_NAMES = {2: "ALB", 3: "VERDE", 4: "GALBEN", 5: "ROSU", 6: "ALBASTRU"}

ALERT_TEXT = {"SOS_EXPLICIT": "AJUTOR SOLICITAT", "MISSED_CHECK_INS": "FARA CONFIRMARE",
              "CHECKIN_TIMEOUT": "FARA CONFIRMARE"}  # fmt: skip
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


def copy_text(source: str | None, size: int) -> str:
    """copyText(): strncpy cu terminator -> cel mult size-1 caractere."""
    return (source or "")[: size - 1]


def strtok(line: str) -> list[str]:
    """strtok_r(line, "|") sare peste câmpurile goale (ex. "A||B" -> ["A", "B"])."""
    return [part for part in line.split("|") if part]


@dataclass
class NanoFirmware:
    """Starea și logica din sketch. `now` este millis(), în ms de la pornirea plăcii."""

    pc_linked: bool = False
    alert_active: bool = False
    technical_error: bool = False
    last_pc_message_at: int = 0
    resolved_display_until: int = 0
    technical_sound_started_at: int = 0
    active_alert_id: str = ""
    alert_reason: str = ""
    technical_reason: str = ""
    safety_state: int = STANDBY
    one_shot_resolved_at: int | None = None
    _buffer: str = ""
    _overflow: bool = False
    output: list[str] = field(default_factory=list)

    # -- setup() --------------------------------------------------------------------------
    def boot(self) -> list[str]:
        return ["BOOT|VESTA_NANO|1"]  # LCD-ul virtual e mereu prezent: fără LCD|MISSING

    # -- readSerial() ---------------------------------------------------------------------
    def feed(self, data: bytes, now: int) -> list[str]:
        out: list[str] = []
        for char in data.decode("ascii", errors="replace"):
            if char == "\r":
                continue
            if char == "\n":
                if self._overflow:
                    out.append("ERR|LINE_TOO_LONG")
                elif self._buffer:
                    out.extend(self.process_command(self._buffer, now))
                self._buffer, self._overflow = "", False
                continue
            if self._overflow:
                continue
            if len(self._buffer) < SERIAL_BUFFER_SIZE - 1:
                self._buffer += char
            else:
                self._overflow = True
        return out

    # -- processCommand() -----------------------------------------------------------------
    def _safety_state_name(self) -> str:
        if self.alert_active:
            return "ALERT"
        return {ACTIVE: "ACTIVE", CHECK: "CHECK"}.get(self.safety_state, "STANDBY")

    def process_command(self, line: str, now: int) -> list[str]:  # noqa: PLR0911, PLR0912
        tokens = strtok(line)
        if not tokens:
            return []
        command, args = tokens[0], tokens[1:]
        arg = lambda i: args[i] if len(args) > i else None  # noqa: E731

        self.pc_linked, self.last_pc_message_at = True, now  # markPcActivity

        if command == "BYE":
            self.pc_linked = False
            if not self.alert_active:
                self.safety_state = STANDBY
            self.technical_error, self.technical_reason = False, ""
            return ["OK|BYE"]
        if command == "HELLO":
            return ["HELLO|VESTA_NANO|1"]
        if command == "PING":
            return [f"PONG|{now}"]
        if command == "STATUS":
            return [
                f"STATUS|PC={int(self.pc_linked)}|STATE={self._safety_state_name()}"
                f"|ALERT={int(self.alert_active)}|TECH={int(self.technical_state_active())}"
                f"|LCD=1|ID={self.active_alert_id}"
            ]
        if command == "READY":
            self.technical_error = False
            return ["OK|READY"]
        if command == "STATE":
            state = arg(0)
            if not state:
                return ["ERR|MISSING_STATE"]
            if self.alert_active:
                return ["ERR|ALERT_ACTIVE"]
            mapping = {"STANDBY": STANDBY, "IDLE": STANDBY, "CANCELLED": STANDBY, "ACTIVE": ACTIVE,
                       "CHECK": CHECK, "CHECK_IN": CHECK, "UNCONFIRMED": CHECK}  # fmt: skip
            if state not in mapping:
                return ["ERR|UNKNOWN_STATE"]
            self.safety_state = mapping[state]
            return [f"OK|STATE|{self._safety_state_name()}"]
        if command == "ALERT_RAISED":
            alert_id, reason = arg(0), arg(1)
            if not alert_id:
                return ["ERR|MISSING_ALERT_ID"]
            self.active_alert_id = copy_text(alert_id, ALERT_ID_SIZE)
            self.alert_reason = copy_text(
                "ALERTA" if reason is None else ALERT_TEXT.get(reason, reason), REASON_SIZE
            )
            self.alert_active, self.safety_state, self.resolved_display_until = True, STANDBY, 0
            return [f"OK|ALERT_RAISED|{self.active_alert_id}"]
        if command == "ALERT_RESOLVED":
            alert_id = arg(0)
            if not alert_id:
                return ["ERR|MISSING_ALERT_ID"]
            # strcmp(alertId, activeAlertId): ID-ul primit e comparat netrunchiat
            if self.alert_active and self.active_alert_id and alert_id != self.active_alert_id:
                return ["ERR|ALERT_ID_MISMATCH"]
            self.active_alert_id = copy_text(alert_id, ALERT_ID_SIZE)
            self.alert_active, self.safety_state = False, STANDBY
            self.resolved_display_until = now + RESOLVED_DISPLAY_MS
            self.one_shot_resolved_at = now
            return [f"OK|ALERT_RESOLVED|{self.active_alert_id}"]
        if command in {"TECHNICAL_ERROR", "DELIVERY_FAILED"}:
            reason = arg(0)
            self.technical_error, self.technical_sound_started_at = True, now
            if command == "DELIVERY_FAILED":
                self.technical_reason = "LIVRARE ESUATA"
            else:
                text = "EROARE TEHNICA" if reason is None else TECH_TEXT.get(reason, reason)
                self.technical_reason = copy_text(text, REASON_SIZE)
            return [f"OK|{command}"]
        if command in {"TECHNICAL_RESOLVED", "DELIVERED"}:
            self.technical_error, self.technical_reason = False, ""
            return [f"OK|{command}"]
        if command == "CLEAR":
            if self.alert_active:
                return ["ERR|ALERT_ACTIVE"]
            self.technical_error, self.technical_reason, self.resolved_display_until = False, "", 0
            return ["OK|CLEAR"]
        return ["ERR|UNKNOWN_COMMAND"]

    # -- updateLink() ---------------------------------------------------------------------
    def update_link(self, now: int) -> list[str]:
        if self.pc_linked and now - self.last_pc_message_at > PC_TIMEOUT_MS:
            self.pc_linked = False
            self.technical_sound_started_at = now
            if self.session_shown():
                return ["LINK|LOST|SESSION"]
            self.technical_error, self.technical_reason = False, ""
            return ["LINK|LOST|IDLE"]
        return []

    # -- stări derivate -------------------------------------------------------------------
    def session_shown(self) -> bool:
        return self.alert_active or self.safety_state != STANDBY

    def link_lost_error(self) -> bool:
        return not self.pc_linked and self.session_shown()

    def waiting_for_pc(self) -> bool:
        return not self.pc_linked and not self.session_shown()

    def technical_state_active(self) -> bool:
        return self.technical_error or self.link_lost_error()

    # -- updateLeds(): ce LED arde și cum (fără faza de clipire) --------------------------
    def led(self) -> tuple[str, str]:
        if self.alert_active:
            return "ROSU", "clipeste rapid (250 ms)"
        if self.technical_state_active():
            return "ALBASTRU", "clipeste lent (600 ms)"
        if self.safety_state == CHECK:
            return "GALBEN", "clipeste lent (600 ms)"
        if self.safety_state == ACTIVE:
            return "VERDE", "aprins"
        if self.waiting_for_pc():
            return "ALB", "aprins, pauza scurta la 2 s"
        return "ALB", "aprins"

    # -- updateLcd() ----------------------------------------------------------------------
    def lcd(self, now: int) -> tuple[str, str]:
        if self.alert_active:
            return "ROSU 03 ALERTA", self.alert_reason[:16]
        if self.technical_state_active():
            line2 = "CONEXIUNE PC" if self.link_lost_error() else (self.technical_reason or "EROARE TEHNICA")
            return "ALBASTRU 04 ERR", line2[:16]
        if self.waiting_for_pc():
            return "ALB 00 STANDBY", "Astept PC"
        if self.resolved_display_until and self.resolved_display_until - now > 0:
            return "ALB 00 STANDBY", "Alerta rezolvata"
        if self.safety_state == CHECK:
            return "GALBEN 02 CHECK", "Astept raspuns"
        if self.safety_state == ACTIVE:
            return "VERDE 01 ACTIV", "Conversatie OK"
        return "ALB 00 STANDBY", "Fara sesiune"

    # -- updateSound() --------------------------------------------------------------------
    def sound(self, now: int) -> str:
        if self.one_shot_resolved_at is not None and now - self.one_shot_resolved_at < 360:
            return "D8 ton scurt de rezolvare"
        if self.alert_active:
            return "D7 alarma (bip-bip-bip)"
        if self.technical_state_active():
            return "D8 eroare tehnica (biiiip lung)"
        return "liniste"


class VirtualNano:
    """Placa virtuală + „cablul USB” (server TCP, o singură conexiune, ca un port serial)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 7701, *, on_change=None, on_line=None):
        self.address = (host, port)
        self.firmware = NanoFirmware()
        self.usb_connected = True  # „cablul” poate fi scos din API pentru testul de reconectare
        self.serial_open = False
        self._boot_at = time.monotonic()
        self._on_change = on_change or (lambda view: None)
        self._on_line = on_line or (lambda direction, text: None)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._conn: socket.socket | None = None
        self._last_view: tuple | None = None
        self.log: list[tuple[float, str, str]] = []  # (timp, '->' sau '<-', linie)

    def millis(self) -> int:
        return int((time.monotonic() - self._boot_at) * 1000)

    def view(self) -> dict:
        with self._lock:
            now = self.millis()
            fw = self.firmware
            led, pattern = fw.led()
            line1, line2 = fw.lcd(now)
            return {
                "usb_connected": self.usb_connected,
                "serial_open": self.serial_open,
                "pc_linked": fw.pc_linked,
                "led": led,
                "led_pattern": pattern,
                "lcd": [line1, line2],
                "buzzer": fw.sound(now),
                "alert_id": fw.active_alert_id if fw.alert_active else None,
                "uptime_ms": now,
            }

    def set_usb(self, connected: bool) -> None:
        self.usb_connected = connected
        if not connected:
            self._close_conn()

    def start(self) -> None:
        threading.Thread(target=self._serve, name="virtual-nano", daemon=True).start()
        threading.Thread(target=self._tick, name="virtual-nano-loop", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._close_conn()

    # -- intern ---------------------------------------------------------------------------
    def _record(self, direction: str, text: str) -> None:
        self.log.append((time.time(), direction, text))
        del self.log[:-200]
        self._on_line(direction, text)

    def _send(self, lines: list[str]) -> None:
        conn = self._conn
        for line in lines:
            self._record("->", line)
            if conn is not None:
                try:
                    conn.sendall((line + "\r\n").encode("ascii"))  # Serial.println trimite \r\n
                except OSError:
                    self._close_conn()
                    return

    def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        self.serial_open = False
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def _changed(self) -> None:
        view = self.view()
        key = (view["led"], view["led_pattern"], tuple(view["lcd"]), view["buzzer"], view["serial_open"])
        if key != self._last_view:
            self._last_view = key
            self._on_change(view)

    def _listen(self) -> socket.socket:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(self.address)
        server.listen(1)
        server.settimeout(0.5)
        return server

    def _serve(self) -> None:
        # Cablu scos = portul nu există: nimeni nu ascultă, deschiderea eșuează (ca un COM dispărut).
        server: socket.socket | None = None
        while not self._stop.is_set():
            if not self.usb_connected:
                if server is not None:
                    server.close()
                    server = None
                self._stop.wait(0.2)
                continue
            if server is None:
                server = self._listen()
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            if self._conn is not None:
                conn.close()  # portul e deja deschis de alt program
                continue
            self._run_connection(conn)
        if server is not None:
            server.close()

    def _run_connection(self, conn: socket.socket) -> None:
        # Deschiderea portului (DTR) resetează Nano-ul: stare nouă, bootloader, apoi setup().
        with self._lock:
            self.firmware = NanoFirmware()
            self._boot_at = time.monotonic()
        self._conn, self.serial_open = conn, True
        conn.settimeout(0.02)
        booted = False
        while not self._stop.is_set() and self._conn is conn:
            try:
                data = conn.recv(512)
                if data == b"":
                    break  # bridge-ul a închis portul
            except TimeoutError:
                data = b""
            except OSError:
                break
            if not booted:
                if time.monotonic() - self._boot_at < BOOTLOADER_S:
                    continue  # octeții trimiși în timpul bootloader-ului se pierd
                booted = True
                self._send(self.firmware.boot())
                continue
            if data:
                for line in data.decode("ascii", errors="replace").splitlines():
                    if line.strip():
                        self._record("<-", line.strip())
                with self._lock:
                    replies = self.firmware.feed(data, self.millis())
                self._send(replies)
            self._changed()
        if self._conn is conn:
            self._close_conn()
        self._changed()

    def _tick(self) -> None:
        # loop(): updateLink + LED/LCD/sunet, chiar și fără conexiune (placa rămâne alimentată)
        while not self._stop.is_set():
            with self._lock:
                lines = self.firmware.update_link(self.millis())
            if lines:
                self._send(lines)
            self._changed()
            time.sleep(0.05)
