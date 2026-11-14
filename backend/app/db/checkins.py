"""Verificări de siguranță. Distinge: nelivrată (REQUESTED → TIMED_OUT, delivered_at NULL),
livrată fără răspuns (DELIVERED → TIMED_OUT) și confirmată (CONFIRMED)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .errors import SessionClosed, SessionNotFound
from .events import as_uuid, claim_event_id, derived_event_id, insert_session_event

OPEN = ("REQUESTED", "DELIVERED", "AUDIO_UNAVAILABLE")
CONFIRMABLE_SESSION_STATES = ("ACTIVE", "CHECK_IN", "UNCONFIRMED")

ConfirmStatus = Literal[
    "CONFIRMED", "DUPLICATE_EVENT", "LATE", "NO_OPEN_CHECKIN", "WRONG_SESSION", "NOT_ALLOWED"
]


@dataclass
class ConfirmResult:
    status: ConfirmStatus
    checkin: dict | None = None


class CheckinsRepo:
    def __init__(self, pool, settings):
        self._pool = pool
        self._s = settings

    def request(
        self,
        session_id,
        *,
        round_no: int,
        attempt_no: int,
        checkin_id=None,
        timeout_s: float | None = None,
        at: datetime | None = None,
    ) -> tuple[bool, dict]:
        """Creează verificarea (idempotent pe session/round/attempt). Întoarce (creată_acum, checkin)."""
        timeout = self._s.checkin_timeout_s if timeout_s is None else timeout_s
        sid, cid = as_uuid(session_id), as_uuid(checkin_id)
        with self._pool.transaction() as tx:
            s = tx.one("SELECT state FROM sessions WHERE session_id = %s FOR UPDATE", (sid,))
            if s is None:
                raise SessionNotFound(session_id)
            if s["state"] not in CONFIRMABLE_SESSION_STATES:
                raise SessionClosed(f"Nu se cer verificări în starea {s['state']}")
            row = tx.one(
                "INSERT INTO safety_checkins "
                "(checkin_id, session_id, round_no, attempt_no, requested_at, expires_at) "
                "VALUES (COALESCE(%s::uuid, gen_random_uuid()), %s, %s, %s, "
                "COALESCE(%s::timestamptz, now()), "
                "COALESCE(%s::timestamptz, now()) + make_interval(secs => %s::double precision)) "
                "ON CONFLICT DO NOTHING RETURNING *",
                (cid, sid, round_no, attempt_no, at, at, timeout),
            )
            if row is None:
                existing = tx.one(
                    "SELECT * FROM safety_checkins WHERE session_id = %s AND "
                    "((round_no = %s AND attempt_no = %s) OR status = ANY(%s::text[])) "
                    "ORDER BY requested_at DESC LIMIT 1",
                    (sid, round_no, attempt_no, list(OPEN)),
                )
                return False, existing
            insert_session_event(
                tx,
                event_id=derived_event_id(row["checkin_id"], "requested"),
                session_id=sid,
                event_type="CHECKIN_REQUESTED",
                source="SAFETY_ENGINE",
                occurred_at=at,
                metadata={
                    "checkin_id": row["checkin_id"],
                    "round_no": round_no,
                    "attempt_no": attempt_no,
                },
            )
            return True, row

    def mark_delivered(
        self, checkin_id, channel: Literal["AUDIO", "COMPANION_UI"], at: datetime | None = None
    ) -> dict | None:
        """Doar când canalul a confirmat livrarea (audio redat / notificare afișată în pagină),
        nu când AI-ul a generat mesajul."""
        with self._pool.transaction() as tx:
            row = tx.one(
                "UPDATE safety_checkins SET status = 'DELIVERED', delivery_channel = %s, "
                "delivered_at = COALESCE(%s::timestamptz, now()) "
                "WHERE checkin_id = %s AND status IN ('REQUESTED', 'AUDIO_UNAVAILABLE') RETURNING *",
                (channel, at, as_uuid(checkin_id)),
            )
            if row:
                insert_session_event(
                    tx,
                    event_id=derived_event_id(checkin_id, "delivered", channel),
                    session_id=row["session_id"],
                    event_type="CHECKIN_DELIVERED",
                    source="SAFETY_ENGINE",
                    occurred_at=at,
                    metadata={"checkin_id": row["checkin_id"], "channel": channel},
                )
            return row

    def mark_audio_unavailable(self, checkin_id, at: datetime | None = None) -> dict | None:
        """Audio indisponibil: verificarea rămâne deschisă, confirmarea poate veni din UI."""
        with self._pool.transaction() as tx:
            row = tx.one(
                "UPDATE safety_checkins SET status = 'AUDIO_UNAVAILABLE' "
                "WHERE checkin_id = %s AND status = 'REQUESTED' RETURNING *",
                (as_uuid(checkin_id),),
            )
            if row:
                insert_session_event(
                    tx,
                    event_id=derived_event_id(checkin_id, "audio_unavailable"),
                    session_id=row["session_id"],
                    event_type="CHECKIN_AUDIO_UNAVAILABLE",
                    source="SAFETY_ENGINE",
                    occurred_at=at,
                    metadata={"checkin_id": row["checkin_id"]},
                )
            return row

    def confirm(
        self,
        session_id,
        *,
        via: Literal["VOICE_TOOL", "UI_BUTTON"],
        event_id: uuid.UUID | str,
        checkin_id=None,
        at: datetime | None = None,
    ) -> ConfirmResult:
        """Validează sesiunea, checkin-ul, momentul și starea. O confirmare întârziată sau
        venită când sesiunea e deja în ALERT NU modifică nimic (doar se înregistrează)."""
        sid, cid = as_uuid(session_id), as_uuid(checkin_id)
        with self._pool.transaction() as tx:
            if not claim_event_id(tx, event_id, "SESSION"):
                return ConfirmResult("DUPLICATE_EVENT")
            row = tx.one(
                """
                UPDATE safety_checkins c SET status = 'CONFIRMED', confirmed_via = %s,
                       confirmed_at = COALESCE(%s::timestamptz, now()), closed_at = COALESCE(%s::timestamptz, now())
                WHERE c.session_id = %s AND (%s::uuid IS NULL OR c.checkin_id = %s::uuid)
                  AND c.status = ANY(%s::text[])
                  AND COALESCE(%s::timestamptz, now()) <= c.expires_at
                  AND EXISTS (SELECT 1 FROM sessions s WHERE s.session_id = c.session_id
                              AND s.state = ANY(%s::text[]))
                RETURNING *
                """,
                (via, at, at, sid, cid, cid, list(OPEN), at, list(CONFIRMABLE_SESSION_STATES)),
            )
            if row:
                status, ev_type, checkin = "CONFIRMED", "CHECKIN_CONFIRMED", row
            else:
                session = tx.one("SELECT state FROM sessions WHERE session_id = %s", (sid,))
                if session is None:
                    raise SessionNotFound(session_id)
                checkin = (
                    tx.one("SELECT * FROM safety_checkins WHERE checkin_id = %s", (cid,))
                    if cid
                    else None
                )
                if checkin is not None and checkin["session_id"] != sid:
                    status, ev_type, checkin = (
                        "WRONG_SESSION",
                        "CHECKIN_CONFIRMATION_REJECTED",
                        None,
                    )
                elif session["state"] not in CONFIRMABLE_SESSION_STATES:
                    status, ev_type = "NOT_ALLOWED", "CHECKIN_CONFIRMATION_REJECTED"
                elif checkin is not None or tx.one(
                    "SELECT 1 AS x FROM safety_checkins WHERE session_id = %s AND status = ANY(%s::text[])",
                    (sid, list(OPEN)),
                ):
                    status, ev_type = "LATE", "CHECKIN_LATE_CONFIRMATION"
                else:
                    status, ev_type = "NO_OPEN_CHECKIN", "SAFETY_CONFIRMED_UNPROMPTED"
            insert_session_event(
                tx,
                event_id=event_id,
                session_id=sid,
                event_type=ev_type,
                source="VOICE" if via == "VOICE_TOOL" else "COMPANION",
                occurred_at=at,
                already_claimed=True,
                metadata={"via": via, "checkin_id": cid, "result": status},
            )
            return ConfirmResult(status, checkin)

    def expire_due(self, at: datetime | None = None) -> list[dict]:
        """Apelat periodic de Safety Engine (temporizarea rulează pe server, nu în browser)."""
        with self._pool.transaction() as tx:
            rows = tx.all(
                "UPDATE safety_checkins SET status = 'TIMED_OUT', closed_at = COALESCE(%s::timestamptz, now()) "
                "WHERE status = ANY(%s::text[]) AND expires_at < COALESCE(%s::timestamptz, now()) RETURNING *",
                (at, list(OPEN), at),
            )
            for r in rows:
                insert_session_event(
                    tx,
                    event_id=derived_event_id(r["checkin_id"], "timeout"),
                    session_id=r["session_id"],
                    event_type="CHECKIN_TIMED_OUT",
                    source="SAFETY_ENGINE",
                    occurred_at=at,
                    metadata={
                        "checkin_id": r["checkin_id"],
                        "attempt_no": r["attempt_no"],
                        "was_delivered": r["delivered_at"] is not None,
                        "delivery_channel": r["delivery_channel"],
                    },
                )
            return rows

    def time_out_open(self, session_id, at: datetime | None = None) -> dict | None:
        """Închide verificarea deschisă când Safety Engine (care ține timpul) a decis că a expirat."""
        with self._pool.transaction() as tx:
            r = tx.one(
                "UPDATE safety_checkins SET status = 'TIMED_OUT', closed_at = COALESCE(%s::timestamptz, now()) "
                "WHERE session_id = %s AND status = ANY(%s::text[]) RETURNING *",
                (at, as_uuid(session_id), list(OPEN)),
            )
            if r:
                insert_session_event(
                    tx,
                    event_id=derived_event_id(r["checkin_id"], "timeout"),
                    session_id=r["session_id"],
                    event_type="CHECKIN_TIMED_OUT",
                    source="SAFETY_ENGINE",
                    occurred_at=at,
                    metadata={
                        "checkin_id": r["checkin_id"],
                        "attempt_no": r["attempt_no"],
                        "was_delivered": r["delivered_at"] is not None,
                        "delivery_channel": r["delivery_channel"],
                    },
                )
            return r

    def open_for_session(self, session_id) -> dict | None:
        with self._pool.autocommit() as tx:
            return tx.one(
                "SELECT * FROM safety_checkins WHERE session_id = %s AND status = ANY(%s::text[])",
                (as_uuid(session_id), list(OPEN)),
            )

    def for_session(self, session_id) -> list[dict]:
        with self._pool.autocommit() as tx:
            return tx.all(
                "SELECT * FROM safety_checkins WHERE session_id = %s ORDER BY requested_at",
                (as_uuid(session_id),),
            )
