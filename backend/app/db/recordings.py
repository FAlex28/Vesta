"""Înregistrările convorbirilor în Tiger Data (audio în bucăți, detaliile alertei, mesajele dispecerului)."""

from __future__ import annotations

from datetime import datetime

from .events import as_uuid

_META = ("device_id", "mime_type", "chunks", "size_bytes", "started_at", "updated_at", "finalized",
         "alert_id", "alert_reason", "alert_at", "resolution", "resolved_at", "operator_at")  # fmt: skip


class RecordingsRepo:
    def __init__(self, pool, settings):
        self._pool = pool
        self._s = settings

    def upsert_meta(self, session_id, fields: dict) -> None:
        values = {key: value for key, value in fields.items() if key in _META and value is not None}
        sid = as_uuid(session_id)
        with self._pool.autocommit() as tx:
            tx.exec("INSERT INTO session_recordings (session_id) VALUES (%s) ON CONFLICT (session_id) DO NOTHING", (sid,))
            if values:
                assignments = ", ".join(f"{key} = %s" for key in values)
                tx.exec(
                    f"UPDATE session_recordings SET {assignments} WHERE session_id = %s",  # noqa: S608 - allow-listed
                    (*values.values(), sid),
                )

    def add_chunk(self, session_id, seq: int, data: bytes, *, mime_type: str, updated_at: datetime) -> None:
        sid = as_uuid(session_id)
        with self._pool.transaction() as tx:
            tx.exec(
                "INSERT INTO session_recordings (session_id, mime_type) VALUES (%s, %s) ON CONFLICT (session_id) DO NOTHING",
                (sid, mime_type),
            )
            inserted = tx.exec(
                "INSERT INTO recording_chunks (session_id, seq, data) VALUES (%s, %s, %s) "
                "ON CONFLICT (session_id, seq) DO NOTHING",
                (sid, seq, bytes(data)),
            )
            if inserted:
                tx.exec(
                    "UPDATE session_recordings SET chunks = chunks + 1, size_bytes = size_bytes + %s, updated_at = %s "
                    "WHERE session_id = %s",
                    (len(data), updated_at, sid),
                )

    def add_operator_message(self, message_id, session_id, at: datetime, mime_type: str, data: bytes) -> None:
        with self._pool.autocommit() as tx:
            tx.exec(
                "INSERT INTO operator_messages (message_id, session_id, at, mime_type, data) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (message_id) DO NOTHING",
                (as_uuid(message_id), as_uuid(session_id), at, mime_type, bytes(data)),
            )

    def delete_session(self, session_id) -> None:
        sid = as_uuid(session_id)
        with self._pool.transaction() as tx:
            for table in ("recording_chunks", "operator_messages", "session_recordings"):
                tx.exec(f"DELETE FROM {table} WHERE session_id = %s", (sid,))  # noqa: S608 - fixed names

    def archive(self, limit: int = 100) -> list[dict]:
        with self._pool.autocommit() as tx:
            return tx.all(
                "SELECT * FROM session_recordings WHERE alert_id IS NOT NULL "
                "AND (resolution IS NULL OR resolution <> 'FALSE_ALARM') "
                "ORDER BY COALESCE(alert_at, started_at) DESC LIMIT %s",
                (limit,),
            )

    def meta(self, session_id) -> dict | None:
        with self._pool.autocommit() as tx:
            return tx.one("SELECT * FROM session_recordings WHERE session_id = %s", (as_uuid(session_id),))

    def audio(self, session_id) -> bytes:
        with self._pool.autocommit() as tx:
            rows = tx.all(
                "SELECT data FROM recording_chunks WHERE session_id = %s ORDER BY seq", (as_uuid(session_id),)
            )
        return b"".join(bytes(row["data"]) for row in rows)
