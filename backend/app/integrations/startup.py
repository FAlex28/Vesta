"""Recuperare la pornirea serverului (după restart Raspberry Pi sau al procesului).

Se apelează o singură dată, din lifespan-ul FastAPI al procesului care deține și monitorul GPIO
(un singur worker uvicorn). Nu creează sesiuni noi și nu presupune că sesiunea vocală continuă."""

from __future__ import annotations

import os
import socket


def recover_on_startup(db) -> dict:
    applied = db.migrate()
    summary = db.sessions.recover_after_restart()
    db.events.log_technical_event(
        component="SERVER", event_type="SERVER_STARTED",
        metadata={
            "host": socket.gethostname(), "pid": os.getpid(), "migrations_applied": applied,
            "sessions_interrupted": [str(s["session_id"]) for s in summary["interrupted"]],
            "sessions_still_in_alert": [str(s["session_id"]) for s in summary["still_in_alert"]],
            "notifications_requeued": summary["notifications_requeued"],
        },
    )
    return {**summary, "migrations_applied": applied}
