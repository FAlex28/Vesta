"""Istoric cronologic: evenimente de sesiune și evenimente tehnice, cu deduplicare după event_id."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from .pool import Tx

# Namespace fix pentru event_id-uri deterministe generate de server (ex. timeout pentru un checkin):
# aceeași cauză produce mereu același event_id, deci o reprocesare nu dublează evenimentul.
VESTA_NS = uuid.UUID("6f1c2a52-9d3e-4c55-9a51-0e7a5e57a001")


def derived_event_id(*parts: Any) -> uuid.UUID:
    return uuid.uuid5(VESTA_NS, ":".join(str(p) for p in parts))


def as_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def claim_event_id(tx: Tx, event_id: uuid.UUID | str, kind: str) -> bool:
    """True dacă event_id nu a mai fost văzut. Se apelează în aceeași tranzacție cu efectul."""
    row = tx.one(
        "INSERT INTO ingested_events (event_id, kind) VALUES (%s, %s) "
        "ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
        (as_uuid(event_id), kind),
    )
    return row is not None


def insert_session_event(
    tx: Tx, *, event_id: uuid.UUID | str, session_id: uuid.UUID | str | None, event_type: str,
    source: str, occurred_at: datetime | None = None, metadata: dict | None = None,
    sequence_number: int | None = None, already_claimed: bool = False,
) -> bool:
    if not already_claimed and not claim_event_id(tx, event_id, "SESSION"):
        return False
    tx.exec(
        "INSERT INTO session_events (event_id, occurred_at, session_id, event_type, source, "
        "sequence_number, metadata) VALUES (%s, COALESCE(%s::timestamptz, now()), %s, %s, %s, %s::int, %s::jsonb) "
        "ON CONFLICT DO NOTHING",
        (as_uuid(event_id), occurred_at, as_uuid(session_id), event_type, source, sequence_number,
         json.dumps(metadata or {}, ensure_ascii=False, default=str)),
    )
    return True


def insert_technical_event(
    tx: Tx, *, component: str, event_type: str, event_id: uuid.UUID | str | None = None,
    session_id: uuid.UUID | str | None = None, client_id: str | None = None,
    occurred_at: datetime | None = None, metadata: dict | None = None,
) -> bool:
    event_id = as_uuid(event_id) or uuid.uuid4()
    if not claim_event_id(tx, event_id, "TECHNICAL"):
        return False
    tx.exec(
        "INSERT INTO technical_events (event_id, occurred_at, component, event_type, session_id, client_id, metadata) "
        "VALUES (%s, COALESCE(%s::timestamptz, now()), %s, %s, %s, %s, %s::jsonb) ON CONFLICT DO NOTHING",
        (event_id, occurred_at, component, event_type, as_uuid(session_id), client_id,
         json.dumps(metadata or {}, ensure_ascii=False, default=str)),
    )
    return True


class EventsRepo:
    def __init__(self, pool):
        self._pool = pool

    def log_session_event(self, **kw) -> bool:
        with self._pool.transaction() as tx:
            return insert_session_event(tx, **kw)

    def log_technical_event(self, **kw) -> bool:
        """Componente: PHONE, PAGE, AUDIO, VOICE_SERVICE, GPS, POWER, SERVER, GPIO.
        Ex.: AUDIO_CONNECTED, AUDIO_DISCONNECTED, AUDIO_ERROR, SERVICE_UNAVAILABLE, SERVICE_RECONNECTED,
        GPS_AVAILABLE, GPS_STALE, GPS_UNAVAILABLE, LOW_VOLTAGE, POWER_UNSTABLE ..."""
        with self._pool.transaction() as tx:
            return insert_technical_event(tx, **kw)

    def timeline(self, session_id, limit: int = 200) -> list[dict]:
        """Istoric unificat (sesiune + tehnic), cel mai recent primul."""
        with self._pool.autocommit() as tx:
            return tx.all(
                """
                SELECT * FROM (
                    SELECT occurred_at, event_id, 'SESSION' AS kind, event_type, source AS origin, metadata
                    FROM session_events WHERE session_id = %s
                    UNION ALL
                    SELECT occurred_at, event_id, 'TECHNICAL', event_type, component, metadata
                    FROM technical_events WHERE session_id = %s
                ) t ORDER BY occurred_at DESC LIMIT %s
                """,
                (as_uuid(session_id), as_uuid(session_id), limit),
            )

    def technical(self, *, component: str | None = None, since_minutes: int = 60, limit: int = 200) -> list[dict]:
        with self._pool.autocommit() as tx:
            return tx.all(
                "SELECT * FROM technical_events WHERE occurred_at > now() - make_interval(mins => %s::int) "
                "AND (%s::text IS NULL OR component = %s::text) ORDER BY occurred_at DESC LIMIT %s",
                (since_minutes, component, component, limit),
            )

    def daily_stats(self, days: int = 7) -> list[dict]:
        """Din agregatul continuu session_events_daily (include și datele încă nematerializate)."""
        with self._pool.autocommit() as tx:
            return tx.all(
                "SELECT day, event_type, events, sessions FROM session_events_daily "
                "WHERE day > now() - make_interval(days => %s::int) ORDER BY day, event_type",
                (days,),
            )
