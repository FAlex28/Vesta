"""Recuperare la pornirea serverului (după restart Raspberry Pi sau al procesului).

Se apelează o singură dată, din lifespan-ul FastAPI al procesului care deține și monitorul GPIO
(un singur worker uvicorn). Nu creează sesiuni noi și nu presupune că sesiunea vocală continuă.
Sincron (rețea): din cod async se apelează cu asyncio.to_thread."""

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
            "alerts_still_active": [str(a["alert_id"]) for a in summary["active_alerts"]],
            "notifications_requeued": summary["notifications_requeued"],
        },
    )
    return {**summary, "migrations_applied": applied}
