"""Worker pentru outbox-ul de notificări. Canalul concret (ex. WebSocket către pagina contactului)
e injectat ca `sender`; worker-ul nu știe nimic despre transport."""

from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger("vesta.outbox")

# sender(notification_row) -> None; ridică excepție dacă transportul a refuzat mesajul.
Sender = Callable[[dict], None]


class OutboxDispatcher:
    def __init__(self, db, sender: Sender, *, batch: int = 10):
        self._db = db
        self._send = sender
        self._batch = batch

    def run_once(self) -> dict:
        sent = failed = 0
        for n in self._db.notifications.claim_due(self._batch):
            try:
                self._send(n)
            except Exception as e:  # transportul a eșuat → reîncercare cu backoff
                row = self._db.notifications.mark_failed(n["notification_id"], f"{type(e).__name__}: {e}")
                failed += 1
                log.warning("notification %s failed (attempt %s, now %s)",
                            n["notification_id"], n["attempt_count"], row and row["status"])
            else:
                self._db.notifications.mark_sent(n["notification_id"])
                sent += 1
        return {"sent": sent, "failed": failed}

    def run_forever(self, stop: threading.Event, interval_s: float = 1.0) -> None:
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("outbox loop error")
            stop.wait(interval_s)
