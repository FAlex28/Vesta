"""Starea alimentării Raspberry Pi din `vcgencmd get_throttled`.

Pi-ul NU poate măsura procentul unei baterii externe obișnuite, deci nu raportăm niciun procent.
Dacă tensiunea scade suficient, Pi-ul se poate opri complet și nicio notificare nu mai e garantată.

Biți relevanți (documentația Raspberry Pi):
  0  under-voltage acum            16 under-voltage de la boot
  1  frecvență ARM limitată acum   17 limitată de la boot
  2  throttling acum               18 throttling de la boot
  3  limită termică soft acum      19 limită termică de la boot
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

PowerState = Literal["POWER_OK", "LOW_VOLTAGE", "POWER_UNSTABLE", "POWER_STATUS_UNKNOWN"]

UNDERVOLT_NOW = 1 << 0
UNDERVOLT_SINCE_BOOT = 1 << 16
THERMAL_MASK = (1 << 1) | (1 << 2) | (1 << 3)


@dataclass(frozen=True)
class PowerStatus:
    state: PowerState
    raw: int | None
    thermal_throttling: bool = False
    detail: str = ""


def parse_throttled(value: int) -> PowerStatus:
    thermal = bool(value & THERMAL_MASK)
    if value & UNDERVOLT_NOW:
        return PowerStatus("LOW_VOLTAGE", value, thermal, "under-voltage detectat acum")
    if value & UNDERVOLT_SINCE_BOOT:
        return PowerStatus("POWER_UNSTABLE", value, thermal, "under-voltage de la ultimul boot")
    return PowerStatus("POWER_OK", value, thermal, "fără under-voltage raportat de firmware")


def _vcgencmd() -> str | None:
    exe = shutil.which("vcgencmd")
    if exe is None:
        return None
    out = subprocess.run([exe, "get_throttled"], capture_output=True, text=True, timeout=3)
    return out.stdout.strip() if out.returncode == 0 else None


def read_power_status(reader: Callable[[], str | None] = _vcgencmd) -> PowerStatus:
    """reader() întoarce ex. 'throttled=0x50005'; injectabil pentru teste / laptop."""
    try:
        text = reader()
    except Exception as e:
        return PowerStatus("POWER_STATUS_UNKNOWN", None, detail=f"citire eșuată: {e}")
    if not text or "=" not in text:
        return PowerStatus("POWER_STATUS_UNKNOWN", None, detail="vcgencmd indisponibil")
    try:
        return parse_throttled(int(text.split("=", 1)[1], 16))
    except ValueError:
        return PowerStatus("POWER_STATUS_UNKNOWN", None, detail=f"format necunoscut: {text!r}")


class PowerMonitor:
    """Înregistrează în technical_events doar schimbările de stare."""

    def __init__(
        self,
        db,
        reader: Callable[[], str | None] = _vcgencmd,
        on_change: Callable[[PowerStatus], None] | None = None,
    ):
        self._db, self._reader = db, reader
        self._notify = on_change or (lambda _s: None)
        self.last: PowerStatus | None = None

    def run_once(self) -> PowerStatus:
        st = read_power_status(self._reader)
        if self.last is None or st.state != self.last.state:
            self._db.events.log_technical_event(
                component="POWER",
                event_type=st.state,
                metadata={
                    "raw": hex(st.raw) if st.raw is not None else None,
                    "thermal_throttling": st.thermal_throttling,
                    "detail": st.detail,
                },
            )
            self._notify(st)
        self.last = st
        return st
