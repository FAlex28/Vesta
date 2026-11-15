"""Conversation recordings, transcripts and the dispatcher's voice messages, in a local database.

The phone records one continuous MediaRecorder stream (her microphone mixed with Vesta's voice and
the dispatcher's messages) and uploads it in numbered chunks. Everything is kept in one SQLite
database (``VESTA_STATE_DIR/vesta.db``): it survives restarts, needs no network or credentials,
and the dispatcher can listen to any past alert. Concatenated, the chunks are a valid webm file,
so the same rows also serve the live stream. A false alarm deletes the session's rows.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.errors import AppError, ConflictError, NotFoundError

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    session_id      TEXT PRIMARY KEY,
    device_id       TEXT,
    mime_type       TEXT NOT NULL,
    chunks          INTEGER NOT NULL DEFAULT 0,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    finalized       INTEGER NOT NULL DEFAULT 0,
    alert_id        TEXT,
    alert_reason    TEXT,
    alert_at        TEXT,
    resolution      TEXT,
    resolved_at     TEXT,
    operator_at     TEXT
);
CREATE TABLE IF NOT EXISTS recording_chunks (
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    data       BLOB NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS transcript (
    turn_id    TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    at         TEXT NOT NULL,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    action     TEXT
);
CREATE INDEX IF NOT EXISTS transcript_session ON transcript (session_id, at);
CREATE TABLE IF NOT EXISTS operator_messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    at         TEXT NOT NULL,
    mime_type  TEXT NOT NULL,
    data       BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS operator_messages_session ON operator_messages (session_id, at);
"""

_ANNOTATIONS = {"device_id", "alert_id", "alert_reason", "alert_at", "resolution", "resolved_at", "operator_at"}


class RecordingTooLarge(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="RECORDING_TOO_LARGE", message="The recording reached its size limit", status_code=413
        )


@dataclass(frozen=True, slots=True)
class RecordingInfo:
    session_id: str
    mime_type: str
    size_bytes: int
    chunks: int
    started_at: datetime
    updated_at: datetime
    finalized: bool
    device_id: str | None = None
    alert_id: str | None = None
    alert_reason: str | None = None
    alert_at: datetime | None = None
    resolution: str | None = None
    resolved_at: datetime | None = None
    operator_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StoredTurn:
    id: str
    role: str
    text: str
    at: datetime
    action: str | None


@dataclass(frozen=True, slots=True)
class OperatorMessage:
    id: str
    at: datetime
    mime_type: str
    data: bytes


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


class RecordingStore:
    def __init__(self, database: Path | None, *, max_bytes: int = 64 * 1024 * 1024) -> None:
        if database is not None:
            database.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(database) if database else ":memory:", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        if database is not None:
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        # A restart ends every running session: their recordings are complete now.
        self._db.execute("UPDATE recordings SET finalized = 1 WHERE finalized = 0")
        self._db.commit()
        self._max_bytes = max_bytes
        self._lock = asyncio.Lock()
        self._conditions: dict[str, asyncio.Condition] = {}
        self._deleted: set[str] = set()

    def close(self) -> None:
        self._db.close()

    def _condition(self, session_id: str) -> asyncio.Condition:
        condition = self._conditions.get(session_id)
        if condition is None:
            condition = self._conditions[session_id] = asyncio.Condition()
        return condition

    async def _notify(self, session_id: str) -> None:
        condition = self._condition(session_id)
        async with condition:
            condition.notify_all()

    def _row(self, session_id: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM recordings WHERE session_id = ?", (session_id,)).fetchone()

    def _ensure_row(self, session_id: str, mime_type: str = "audio/webm") -> sqlite3.Row:
        row = self._row(session_id)
        if row is None:
            instant = _iso()
            self._db.execute(
                "INSERT INTO recordings (session_id, mime_type, started_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, (mime_type or "audio/webm")[:80], instant, instant),
            )
            row = self._row(session_id)
        assert row is not None
        return row

    # ------------------------------------------------------------------ audio

    async def append(
        self, session_id: str, sequence: int, data: bytes, *, mime_type: str, now: datetime | None = None
    ) -> RecordingInfo:
        """Append chunk ``sequence`` (0, 1, 2…). A repeated chunk is ignored; a gap is refused."""

        async with self._lock:
            if session_id in self._deleted:
                raise ConflictError("RECORDING_FINALIZED", "The session's recording was deleted")
            row = self._row(session_id)
            if (row is None or row["chunks"] == 0) and sequence != 0:
                raise ConflictError("RECORDING_GAP", "The first chunk of a recording must be 0")
            row = self._ensure_row(session_id, mime_type)
            if row["chunks"] == 0:
                self._db.execute(
                    "UPDATE recordings SET mime_type = ?, started_at = ? WHERE session_id = ?",
                    ((mime_type or "audio/webm")[:80], _iso(now), session_id),
                )
            if row["finalized"]:
                raise ConflictError("RECORDING_FINALIZED", "The session's recording is closed")
            if sequence < row["chunks"]:
                return _info(row)  # retry of a chunk already stored
            if sequence > row["chunks"]:
                raise ConflictError("RECORDING_GAP", f"Expected chunk {row['chunks']}, received {sequence}")
            if row["size_bytes"] + len(data) > self._max_bytes:
                raise RecordingTooLarge()
            self._db.execute(
                "INSERT INTO recording_chunks (session_id, seq, data) VALUES (?, ?, ?)",
                (session_id, sequence, sqlite3.Binary(data)),
            )
            self._db.execute(
                "UPDATE recordings SET chunks = chunks + 1, size_bytes = size_bytes + ?, updated_at = ? "
                "WHERE session_id = ?",
                (len(data), _iso(now), session_id),
            )
            self._db.commit()
            info = _info(self._row(session_id))
        await self._notify(session_id)
        return info

    async def finalize(self, session_id: str) -> None:
        async with self._lock:
            self._db.execute("UPDATE recordings SET finalized = 1 WHERE session_id = ?", (session_id,))
            self._db.commit()
        await self._notify(session_id)

    async def annotate(self, session_id: str, **fields: object) -> None:
        """Alert facts kept with the recording, so the archive stays readable after a restart."""

        values = {
            key: (_iso(value) if isinstance(value, datetime) else value)
            for key, value in fields.items()
            if key in _ANNOTATIONS and value is not None
        }
        if not values:
            return
        async with self._lock:
            if session_id in self._deleted:
                return
            self._ensure_row(session_id)
            assignments = ", ".join(f"{key} = ?" for key in values)
            self._db.execute(
                f"UPDATE recordings SET {assignments} WHERE session_id = ?",  # noqa: S608 - allow-listed keys
                (*values.values(), session_id),
            )
            self._db.commit()

    async def delete(self, session_id: str) -> bool:
        """False alarm: audio, transcript and the dispatcher's messages are gone for good."""

        async with self._lock:
            self._deleted.add(session_id)
            existed = self._row(session_id) is not None
            for table in ("recording_chunks", "recordings", "transcript", "operator_messages"):
                self._db.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))  # noqa: S608
            self._db.commit()
        await self._notify(session_id)
        self._conditions.pop(session_id, None)
        return existed

    # ------------------------------------------------------------------ reading

    def info(self, session_id: str) -> RecordingInfo | None:
        """The recording, once it has audio."""

        row = self._row(session_id)
        return _info(row) if row and row["chunks"] > 0 else None

    def record(self, session_id: str) -> RecordingInfo | None:
        """The row with its alert facts, even before the first audio chunk."""

        row = self._row(session_id)
        return _info(row) if row else None

    def archive(self, limit: int = 100) -> list[RecordingInfo]:
        """Sessions that became alerts (never false alarms), newest first."""

        rows = self._db.execute(
            "SELECT * FROM recordings WHERE alert_id IS NOT NULL "
            "AND (resolution IS NULL OR resolution <> 'FALSE_ALARM') "
            "ORDER BY COALESCE(alert_at, started_at) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_info(row) for row in rows]

    async def follow(self, session_id: str, *, idle_timeout: float = 30.0) -> AsyncIterator[bytes]:
        """The audio recorded so far, then new chunks as they arrive, until the recording closes."""

        if self.info(session_id) is None:
            raise NotFoundError("Recording not found")
        position = 0
        condition = self._condition(session_id)
        while True:
            if session_id in self._deleted:
                return
            rows = self._db.execute(
                "SELECT seq, data FROM recording_chunks WHERE session_id = ? AND seq >= ? ORDER BY seq",
                (session_id, position),
            ).fetchall()
            for row in rows:
                if session_id in self._deleted:
                    return
                position = row["seq"] + 1
                yield bytes(row["data"])
            async with condition:
                row = self._row(session_id)
                if row is None:
                    return
                if position < row["chunks"]:
                    continue
                if row["finalized"]:
                    return
                try:
                    await asyncio.wait_for(condition.wait(), timeout=idle_timeout)
                except TimeoutError:
                    return

    # ------------------------------------------------------------------ transcript

    def add_turn(self, session_id: str, turn) -> None:
        if session_id in self._deleted:
            return
        self._db.execute(
            "INSERT OR IGNORE INTO transcript (turn_id, session_id, at, role, text, action) VALUES (?, ?, ?, ?, ?, ?)",
            (turn.id, session_id, _iso(turn.at), turn.role, turn.text, turn.action),
        )
        self._db.commit()

    def turns(self, session_id: str) -> list[StoredTurn]:
        rows = self._db.execute("SELECT * FROM transcript WHERE session_id = ? ORDER BY at", (session_id,)).fetchall()
        return [StoredTurn(row["turn_id"], row["role"], row["text"], _dt(row["at"]), row["action"]) for row in rows]

    # ------------------------------------------------------------------ dispatcher's voice

    async def add_operator_message(self, session_id: str, data: bytes, mime_type: str) -> OperatorMessage:
        message = OperatorMessage(str(uuid4()), datetime.now(UTC), mime_type, data)
        async with self._lock:
            if session_id in self._deleted:
                raise ConflictError("SESSION_DELETED", "The session was a false alarm")
            self._db.execute(
                "INSERT INTO operator_messages (message_id, session_id, at, mime_type, data) VALUES (?, ?, ?, ?, ?)",
                (message.id, session_id, _iso(message.at), mime_type, sqlite3.Binary(data)),
            )
            self._db.execute(
                "INSERT OR IGNORE INTO transcript (turn_id, session_id, at, role, text, action) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (message.id, session_id, _iso(message.at), "operator", "Mesaj vocal — transcriere indisponibilă."),
            )
            self._db.commit()
        return message

    def operator_messages(self, session_id: str, *, after: str | None = None) -> list[OperatorMessage]:
        since = ""
        if after:
            row = self._db.execute(
                "SELECT at FROM operator_messages WHERE message_id = ? AND session_id = ?", (after, session_id)
            ).fetchone()
            since = row["at"] if row else ""
        rows = self._db.execute(
            "SELECT * FROM operator_messages WHERE session_id = ? AND at > ? ORDER BY at", (session_id, since)
        ).fetchall()
        return [OperatorMessage(row["message_id"], _dt(row["at"]), row["mime_type"], bytes(row["data"])) for row in rows]


def _info(row: sqlite3.Row) -> RecordingInfo:
    return RecordingInfo(
        session_id=row["session_id"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        chunks=row["chunks"],
        started_at=_dt(row["started_at"]),
        updated_at=_dt(row["updated_at"]),
        finalized=bool(row["finalized"]),
        device_id=row["device_id"],
        alert_id=row["alert_id"],
        alert_reason=row["alert_reason"],
        alert_at=_dt(row["alert_at"]),
        resolution=row["resolution"],
        resolved_at=_dt(row["resolved_at"]),
        operator_at=_dt(row["operator_at"]),
    )
