"""Conversația cu asistentul din căști (doar text; audio rămâne în fișierul înregistrării)."""

from __future__ import annotations

from datetime import datetime

from .events import as_uuid


class ConversationsRepo:
    def __init__(self, pool, settings):
        self._pool = pool
        self._s = settings

    def add_turn(
        self,
        *,
        turn_id: str,
        session_id: str,
        occurred_at: datetime,
        role: str,
        text: str,
        action: str | None = None,
    ) -> None:
        with self._pool.autocommit() as tx:
            tx.exec(
                "INSERT INTO conversation_turns (turn_id, session_id, occurred_at, role, text, action) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (turn_id) DO NOTHING",
                (as_uuid(turn_id), as_uuid(session_id), occurred_at, role.upper(), text, action),
            )

    def for_session(self, session_id) -> list[dict]:
        with self._pool.autocommit() as tx:
            return tx.all(
                "SELECT turn_id, occurred_at, role, text, action FROM conversation_turns "
                "WHERE session_id = %s ORDER BY occurred_at",
                (as_uuid(session_id),),
            )

    def delete_for_session(self, session_id) -> int:
        with self._pool.autocommit() as tx:
            rows = tx.all(
                "DELETE FROM conversation_turns WHERE session_id = %s RETURNING 1",
                (as_uuid(session_id),),
            )
        return len(rows)
