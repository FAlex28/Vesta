"""Monitor de conectivitate pentru telefoanele companion. Rulează pe server: nu depinde de
timerele browserului (care se opresc când pagina e suspendată)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger("vesta.heartbeat")


class HeartbeatMonitor:
    def __init__(self, db, on_transition: Callable[[dict], None] | None = None):
        self._db = db
        self._notify = on_transition or (lambda _row: None)

    def run_once(self) -> list[dict]:
        rows = self._db.companions.sweep()
        for r in rows:
            # publicat către dashboard / Safety Engine; NU generează SOS
            self._notify(r)
        return rows

    def run_forever(self, stop: threading.Event, interval_s: float = 5.0) -> None:
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("heartbeat sweep error")
            stop.wait(interval_s)
