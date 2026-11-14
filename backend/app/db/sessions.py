"""Persistența sesiunilor. Mașina de stări autoritativă e Safety Engine (backend/app/safety);
aici doar persistăm tranzițiile atomic și refuzăm combinațiile imposibile."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .errors import AlertStillActive, InvalidTransition, SessionClosed, SessionNotFound, StaleVersion
from .events import as_uuid, claim_event_id, derived_event_id, insert_session_event
from .pool import Tx

OPEN_STATES = ("ACTIVE", "CHECK_IN", "UNCONFIRMED", "ALERT")
TERMINAL_STATES = ("CANCELLED", "INTERRUPTED")

# Tranziții permise la nivel de persistență (plasă de siguranță; regulile fine sunt în Safety Engine).
ALLOWED_FROM: dict[str, tuple[str, ...]] = {
    "ACTIVE":      ("CHECK_IN", "UNCONFIRMED"),
    "CHECK_IN":    ("ACTIVE", "UNCONFIRMED"),
    "UNCONFIRMED": ("ACTIVE", "CHECK_IN"),
    "ALERT":       ("ACTIVE", "CHECK_IN", "UNCONFIRMED"),
    "CANCELLED":   ("ACTIVE", "CHECK_IN", "UNCONFIRMED", "ALERT"),
    "INTERRUPTED": ("ACTIVE", "CHECK_IN", "UNCONFIRMED"),
}

ActivationStatus = Literal["CREATED", "DUPLICATE_EVENT", "ALREADY_ACTIVE"]


@dataclass
class ActivationResult:
    status: ActivationStatus
    session: dict


@dataclass
class CancelResult:
    session: dict
    quick_cancel: bool       # anulată în fereastra rapidă de după activare
    duplicate: bool = False


def _get(tx: Tx, session_id, *, lock: bool = False) -> dict | None:
    return tx.one(
        "SELECT * FROM sessions WHERE session_id = %s" + (" FOR UPDATE" if lock else ""),
        (as_uuid(session_id),),
    )


class SessionsRepo:
    def __init__(self, pool, settings):
        self._pool = pool
        self._s = settings

    # ---------- activare ----------

    def activate(
        self, owner_id: str, activation_event_id: uuid.UUID | str, *,
        occurred_at: datetime | None = None, simulated: bool = False,
        cancel_window_s: float | None = None,
    ) -> ActivationResult:
        """Apelat O SINGURĂ DATĂ per apăsare validă (după cele 5 s), cu event_id generat de monitorul GPIO.
        Idempotent: același event_id → DUPLICATE_EVENT; sesiune deja deschisă → ALREADY_ACTIVE."""
        ev = as_uuid(activation_event_id)
        window = self._s.cancel_window_s if cancel_window_s is None else cancel_window_s
        with self._pool.transaction() as tx:
            existing = tx.one("SELECT * FROM sessions WHERE activation_event_id = %s", (ev,))
            if existing:
                return ActivationResult("DUPLICATE_EVENT", existing)

            created = tx.one(
                "INSERT INTO sessions (owner_id, activation_event_id, cancel_window_until, simulated) "
                "VALUES (%s, %s, now() + make_interval(secs => %s::double precision), %s) "
                "ON CONFLICT DO NOTHING RETURNING *",
                (owner_id, ev, window, simulated),
            )
            if created:
                insert_session_event(
                    tx, event_id=ev, session_id=created["session_id"], event_type="SESSION_ACTIVATED",
                    source="GPIO", occurred_at=occurred_at, sequence_number=created["state_version"],
                    metadata={"cancel_window_s": window, "simulated": simulated},
                )
                return ActivationResult("CREATED", created)

            # conflict: fie același event_id a câștigat cursa, fie există deja o sesiune deschisă
            existing = tx.one("SELECT * FROM sessions WHERE activation_event_id = %s", (ev,))
            if existing:
                return ActivationResult("DUPLICATE_EVENT", existing)
            current = tx.one(
                "SELECT * FROM sessions WHERE owner_id = %s AND state = ANY(%s::text[])",
                (owner_id, list(OPEN_STATES)),
            )
            insert_session_event(
                tx, event_id=ev, session_id=current["session_id"], event_type="REACTIVATION_IGNORED",
                source="GPIO", occurred_at=occurred_at, metadata={"state": current["state"]},
            )
            return ActivationResult("ALREADY_ACTIVE", current)

    # ---------- tranziții ----------

    def transition(
        self, session_id, to_state: str, *, event_id: uuid.UUID | str, source: str = "SAFETY_ENGINE",
        expected_version: int | None = None, reason: str | None = None, metadata: dict | None = None,
        occurred_at: datetime | None = None,
    ) -> dict:
        """Tranziție atomică, cu control optimist pe state_version. Retrimiterea aceluiași event_id
        nu aplică tranziția a doua oară (întoarce starea curentă)."""
        if to_state not in ALLOWED_FROM:
            raise InvalidTransition(f"Stare necunoscută: {to_state}")
        with self._pool.transaction() as tx:
            if not claim_event_id(tx, event_id, "SESSION"):
                cur = _get(tx, session_id)
                if cur is None:
                    raise SessionNotFound(session_id)
                return cur
            return self._transition_tx(tx, session_id, to_state, event_id=event_id, source=source,
                                       expected_version=expected_version, reason=reason,
                                       metadata=metadata, occurred_at=occurred_at)

    @staticmethod
    def _transition_tx(tx: Tx, session_id, to_state: str, *, event_id, source, expected_version=None,
                       reason=None, metadata=None, occurred_at=None) -> dict:
        terminal = to_state in TERMINAL_STATES
        row = tx.one(
            "UPDATE sessions SET state = %s, "
            "ended_at = CASE WHEN %s THEN now() ELSE ended_at END, "
            "end_reason = CASE WHEN %s THEN COALESCE(%s::text, end_reason) ELSE end_reason END "
            "WHERE session_id = %s AND state = ANY(%s::text[]) "
            "AND (%s::int IS NULL OR state_version = %s::int) RETURNING *",
            (to_state, terminal, terminal, reason, as_uuid(session_id), list(ALLOWED_FROM[to_state]),
             expected_version, expected_version),
        )
        if row is None:
            cur = _get(tx, session_id)
            if cur is None:
                raise SessionNotFound(session_id)
            if cur["state"] in TERMINAL_STATES:
                raise SessionClosed(f"Sesiunea e {cur['state']}")
            if expected_version is not None and cur["state_version"] != expected_version:
                raise StaleVersion(f"versiune {cur['state_version']} ≠ {expected_version}")
            raise InvalidTransition(f"{cur['state']} → {to_state} nu e permis")
        insert_session_event(
            tx, event_id=event_id, session_id=row["session_id"], event_type=f"STATE_{to_state}",
            source=source, occurred_at=occurred_at, sequence_number=row["state_version"],
            metadata={**(metadata or {}), **({"reason": reason} if reason else {})}, already_claimed=True,
        )
        return row

    def cancel(
        self, session_id, *, event_id: uuid.UUID | str, source: str = "COMPANION",
        reason: str | None = None,
    ) -> CancelResult:
        """Încheie sesiunea. reason='ACCIDENTAL_ACTIVATION' dacă interfața îl trimite.
        Refuză dacă există o alertă activă: aceea se închide explicit cu AlertsRepo.resolve()."""
        with self._pool.transaction() as tx:
            cur = _get(tx, session_id, lock=True)
            if cur is None:
                raise SessionNotFound(session_id)
            if not claim_event_id(tx, event_id, "SESSION"):
                return CancelResult(cur, quick_cancel=False, duplicate=True)
            if cur["state"] in TERMINAL_STATES:
                raise SessionClosed(f"Sesiunea e deja {cur['state']}")
            if tx.one("SELECT 1 AS x FROM alerts WHERE session_id = %s AND status = 'ACTIVE'",
                      (cur["session_id"],)):
                raise AlertStillActive("Alerta activă trebuie rezolvată explicit")
            quick = tx.one(
                "SELECT now() <= %s::timestamptz AS q", (cur["cancel_window_until"],)
            )["q"] if cur["cancel_window_until"] else False
            row = self._transition_tx(
                tx, session_id, "CANCELLED", event_id=event_id, source=source,
                reason=reason or "USER_ENDED", metadata={"cancel_type": "QUICK" if quick else "NORMAL"},
            )
            tx.exec(
                "UPDATE safety_checkins SET status = 'CANCELLED', closed_at = now() "
                "WHERE session_id = %s AND status IN ('REQUESTED', 'DELIVERED', 'AUDIO_UNAVAILABLE')",
                (row["session_id"],),
            )
            return CancelResult(row, quick_cancel=bool(quick))

    # ---------- citire ----------

    def get(self, session_id) -> dict | None:
        with self._pool.autocommit() as tx:
            return _get(tx, session_id)

    def get_open(self, owner_id: str) -> dict | None:
        with self._pool.autocommit() as tx:
            return tx.one(
                "SELECT * FROM sessions WHERE owner_id = %s AND state = ANY(%s::text[])",
                (owner_id, list(OPEN_STATES)),
            )

    def list_recent(self, owner_id: str | None = None, limit: int = 20) -> list[dict]:
        with self._pool.autocommit() as tx:
            return tx.all(
                "SELECT * FROM sessions WHERE (%s::text IS NULL OR owner_id = %s::text) "
                "ORDER BY started_at DESC LIMIT %s",
                (owner_id, owner_id, limit),
            )

    # ---------- restart ----------

    def recover_after_restart(self) -> dict:
        """La pornirea serverului. Sesiunile ACTIVE/CHECK_IN/UNCONFIRMED devin INTERRUPTED (conversația
        și temporizările s-au pierdut). Sesiunile în ALERT rămân în ALERT: o alertă nu se închide
        prin restart. Nu creează nicio sesiune nouă. Idempotent."""
        with self._pool.transaction() as tx:
            interrupted = tx.all(
                """
                WITH prev AS (
                    SELECT session_id, state FROM sessions
                    WHERE state IN ('ACTIVE', 'CHECK_IN', 'UNCONFIRMED') FOR UPDATE
                )
                UPDATE sessions s SET state = 'INTERRUPTED', ended_at = now(), end_reason = 'SERVER_RESTART'
                FROM prev WHERE s.session_id = prev.session_id
                RETURNING s.*, prev.state AS previous_state
                """
            )
            for s in interrupted:
                insert_session_event(
                    tx, event_id=derived_event_id(s["session_id"], "interrupted"), session_id=s["session_id"],
                    event_type="SESSION_INTERRUPTED", source="SYSTEM", sequence_number=s["state_version"],
                    metadata={"previous_state": s["previous_state"], "reason": "SERVER_RESTART"},
                )
            tx.exec(
                "UPDATE safety_checkins SET status = 'CANCELLED', closed_at = now() "
                "WHERE status IN ('REQUESTED', 'DELIVERED', 'AUDIO_UNAVAILABLE')"
            )
            # notificările rămase „în zbor” se reîncearcă; cele SENT/DELIVERED/VIEWED nu se ating
            requeued = tx.exec(
                "UPDATE notification_attempts SET status = 'FAILED', next_attempt_at = now(), "
                "last_error = 'interrupted by server restart', updated_at = now() WHERE status = 'SENDING'"
            )
            tx.exec(
                "UPDATE companion_clients SET connection_state = 'RECONNECTING', updated_at = now() "
                "WHERE connection_state = 'CONNECTED'"
            )
            still_alert = tx.all("SELECT * FROM sessions WHERE state = 'ALERT'")
        return {"interrupted": interrupted, "still_in_alert": still_alert, "notifications_requeued": requeued}
