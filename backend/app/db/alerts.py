"""Alert Manager (persistență): o singură alertă logică activă per sesiune, rezolvare explicită,
notificări puse în outbox în aceeași tranzacție."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .errors import AlertNotFound, SessionClosed, SessionNotFound
from .events import as_uuid, claim_event_id, insert_session_event
from .locations import location_status
from .sessions import TERMINAL_STATES

IncidentType = Literal["SOS_EXPLICIT", "CHECKIN_TIMEOUT", "PHONE_DISCONNECTED"]


@dataclass
class RaiseResult:
    status: Literal["CREATED", "MERGED", "DUPLICATE_EVENT"]
    alert: dict | None
    notifications_queued: list[dict] = field(default_factory=list)


@dataclass
class ResolveResult:
    status: Literal["RESOLVED", "ALREADY_RESOLVED", "DUPLICATE_EVENT"]
    alert: dict
    notifications_queued: list[dict] = field(default_factory=list)


class AlertsRepo:
    def __init__(self, pool, settings):
        self._pool = pool
        self._s = settings

    def raise_alert(
        self, session_id, incident_type: IncidentType, *, event_id: uuid.UUID | str,
        source: str = "SAFETY_ENGINE", recipients: list[str] | None = None,
        occurred_at: datetime | None = None, payload: dict | None = None,
    ) -> RaiseResult:
        """SOS explicit sau timeout de verificare. Dacă există deja o alertă activă pe sesiune,
        motivul se adaugă la aceeași alertă (MERGED) și NU se trimite o a doua notificare.
        Trece sesiunea în ALERT în aceeași tranzacție."""
        sid = as_uuid(session_id)
        with self._pool.transaction() as tx:
            session = tx.one("SELECT * FROM sessions WHERE session_id = %s FOR UPDATE", (sid,))
            if session is None:
                raise SessionNotFound(session_id)
            if not claim_event_id(tx, event_id, "ALERT"):
                alert = tx.one(
                    "SELECT * FROM alerts WHERE session_id = %s ORDER BY created_at DESC LIMIT 1", (sid,)
                )
                return RaiseResult("DUPLICATE_EVENT", alert)
            if session["state"] in TERMINAL_STATES:
                raise SessionClosed(f"Sesiunea e {session['state']}")

            loc = tx.one(
                "SELECT occurred_at, lat, lon, accuracy_m, simulated FROM locations "
                "WHERE session_id = %s ORDER BY occurred_at DESC LIMIT 1", (sid,),
            )
            now = tx.one("SELECT now() AS n")["n"]
            snapshot = location_status(loc, now, self._s) if loc else {"status": "UNAVAILABLE"}

            alert = tx.one(
                """
                INSERT INTO alerts (session_id, incident_type, triggers, location_snapshot)
                VALUES (%s, %s, ARRAY[%s]::text[], %s::jsonb)
                ON CONFLICT (session_id) WHERE status = 'ACTIVE' DO UPDATE
                    SET triggers = (SELECT array_agg(DISTINCT t ORDER BY t)
                                    FROM unnest(alerts.triggers || EXCLUDED.triggers) AS t)
                RETURNING *, (xmax = 0) AS inserted
                """,
                (sid, incident_type, incident_type, json.dumps(snapshot, default=str)),
            )
            created = alert.pop("inserted")

            if session["state"] != "ALERT":
                tx.exec("UPDATE sessions SET state = 'ALERT' WHERE session_id = %s", (sid,))
            tx.exec(
                "UPDATE safety_checkins SET status = 'CANCELLED', closed_at = now() "
                "WHERE session_id = %s AND status IN ('REQUESTED', 'DELIVERED', 'AUDIO_UNAVAILABLE')",
                (sid,),
            )
            insert_session_event(
                tx, event_id=event_id, session_id=sid, source=source, occurred_at=occurred_at,
                event_type="ALERT_RAISED" if created else "ALERT_TRIGGER_ADDED", already_claimed=True,
                metadata={"alert_id": alert["alert_id"], "incident_type": incident_type},
            )

            queued: list[dict] = []
            if created:
                body = {"alert_id": str(alert["alert_id"]), "session_id": str(sid),
                        "incident_type": incident_type, "location": snapshot, **(payload or {})}
                for r in recipients or []:
                    n = tx.one(
                        "INSERT INTO notification_attempts (alert_id, kind, recipient_id, max_attempts, payload) "
                        "VALUES (%s, 'ALERT_RAISED', %s, %s, %s::jsonb) ON CONFLICT DO NOTHING RETURNING *",
                        (alert["alert_id"], r, self._s.notify_max_attempts, json.dumps(body, default=str)),
                    )
                    if n:
                        queued.append(n)
            return RaiseResult("CREATED" if created else "MERGED", alert, queued)

    def resolve(
        self, alert_id, *, event_id: uuid.UUID | str, resolved_by: str, reason: str,
        source: str = "COMPANION", end_session: bool = True,
    ) -> ResolveResult:
        """Închidere explicită (apelantul a verificat deja tokenul). Alerta nu se șterge: devine RESOLVED,
        iar contactele care au primit ALERT_RAISED primesc ALERT_RESOLVED."""
        aid = as_uuid(alert_id)
        with self._pool.transaction() as tx:
            current = tx.one("SELECT * FROM alerts WHERE alert_id = %s FOR UPDATE", (aid,))
            if current is None:
                raise AlertNotFound(alert_id)
            if not claim_event_id(tx, event_id, "ALERT"):
                return ResolveResult("DUPLICATE_EVENT", current)
            if current["status"] == "RESOLVED":
                return ResolveResult("ALREADY_RESOLVED", current)
            alert = tx.one(
                "UPDATE alerts SET status = 'RESOLVED', resolved_at = now(), resolved_by = %s, "
                "resolution_reason = %s WHERE alert_id = %s RETURNING *",
                (resolved_by, reason, aid),
            )
            queued = tx.all(
                """
                INSERT INTO notification_attempts (alert_id, kind, channel, recipient_id, max_attempts, payload)
                SELECT DISTINCT alert_id, 'ALERT_RESOLVED', channel, recipient_id, %s::int, %s::jsonb
                FROM notification_attempts WHERE alert_id = %s AND kind = 'ALERT_RAISED'
                ON CONFLICT DO NOTHING RETURNING *
                """,
                (self._s.notify_max_attempts,
                 json.dumps({"alert_id": str(aid), "session_id": str(alert["session_id"]),
                             "resolved_at": alert["resolved_at"], "reason": reason}, default=str),
                 aid),
            )
            insert_session_event(
                tx, event_id=event_id, session_id=alert["session_id"], event_type="ALERT_RESOLVED",
                source=source, already_claimed=True,
                metadata={"alert_id": aid, "resolved_by": resolved_by, "reason": reason},
            )
            if end_session:
                tx.exec(
                    "UPDATE sessions SET state = 'CANCELLED', ended_at = now(), end_reason = 'ALERT_RESOLVED' "
                    "WHERE session_id = %s AND state = 'ALERT'",
                    (alert["session_id"],),
                )
            return ResolveResult("RESOLVED", alert, queued)

    def get(self, alert_id) -> dict | None:
        with self._pool.autocommit() as tx:
            return tx.one("SELECT * FROM alerts WHERE alert_id = %s", (as_uuid(alert_id),))

    def active_for_session(self, session_id) -> dict | None:
        with self._pool.autocommit() as tx:
            return tx.one("SELECT * FROM alerts WHERE session_id = %s AND status = 'ACTIVE'",
                          (as_uuid(session_id),))

    def list(self, *, session_id=None, status: str | None = None, limit: int = 50) -> list[dict]:
        with self._pool.autocommit() as tx:
            return tx.all(
                "SELECT * FROM alerts WHERE (%s::uuid IS NULL OR session_id = %s::uuid) "
                "AND (%s::text IS NULL OR status = %s::text) ORDER BY created_at DESC LIMIT %s",
                (as_uuid(session_id), as_uuid(session_id), status, status, limit),
            )
