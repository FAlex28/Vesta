from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: f"srv-{uuid4()}")
    sequence: int
    type: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    session_id: str | None = None
    device_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class EventBroker:
    """Non-blocking in-process fan-out for dashboard WebSocket clients."""

    def __init__(self, *, queue_size: int = 256, history_size: int = 256) -> None:
        self._queue_size = queue_size
        self._history: deque[EventEnvelope] = deque(maxlen=history_size)
        self._subscribers: set[asyncio.Queue[EventEnvelope]] = set()
        self._sequence = 0
        self._lock = asyncio.Lock()

    async def publish(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        device_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> EventEnvelope:
        async with self._lock:
            self._sequence += 1
            event = EventEnvelope(
                sequence=self._sequence,
                type=event_type,
                session_id=session_id,
                device_id=device_id,
                data=data or {},
            )
            self._history.append(event)
            subscribers = tuple(self._subscribers)

        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow dashboard must never delay safety transitions. Keep the newest
                # event; the client is also told to fetch /status for a fresh snapshot.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                resync = EventEnvelope(
                    sequence=event.sequence,
                    type="stream.resync_required",
                    session_id=event.session_id,
                    device_id=event.device_id,
                    data={"latest_sequence": event.sequence},
                )
                try:
                    queue.put_nowait(resync)
                except asyncio.QueueFull:
                    pass
        return event

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[EventEnvelope]]:
        queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    async def recent(self, *, limit: int = 50) -> list[EventEnvelope]:
        async with self._lock:
            return list(self._history)[-limit:]

    async def current_sequence(self) -> int:
        async with self._lock:
            return self._sequence
