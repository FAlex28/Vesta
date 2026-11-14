from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class VoiceConnectionStatus(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    API_ERROR = "API_ERROR"
    MICROPHONE_ERROR = "MICROPHONE_ERROR"
    TIMEOUT = "TIMEOUT"
    RECONNECTING = "RECONNECTING"
    RECONNECTED = "RECONNECTED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


@dataclass(slots=True)
class VoiceSessionRecord:
    session_id: str
    status: VoiceConnectionStatus = VoiceConnectionStatus.DISCONNECTED
    conversation_id: str | None = None
    updated_at: datetime | None = None
    reservation_expires_at: datetime | None = None
    retry_after: datetime | None = None
    consecutive_failures: int = 0
    version: int = 0


class VoiceConversationError(RuntimeError):
    pass


class VoiceConversationAlreadyActive(VoiceConversationError):
    pass


class VoiceRetryLater(VoiceConversationError):
    def __init__(self, retry_after: datetime) -> None:
        super().__init__("Voice reconnect backoff is still active")
        self.retry_after = retry_after


class VoiceConversationRegistry:
    """Prevents duplicate ElevenLabs conversations for a Vesta session."""

    def __init__(
        self,
        *,
        reservation_ttl: timedelta = timedelta(seconds=30),
        retry_base: timedelta = timedelta(milliseconds=250),
        retry_cap: timedelta = timedelta(seconds=5),
    ) -> None:
        if reservation_ttl <= timedelta(0):
            raise ValueError("reservation_ttl must be positive")
        if retry_base <= timedelta(0) or retry_cap < retry_base:
            raise ValueError("invalid retry interval configuration")
        self._reservation_ttl = reservation_ttl
        self._retry_base = retry_base
        self._retry_cap = retry_cap
        self._records: dict[str, VoiceSessionRecord] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, session_id: str, *, now: datetime) -> VoiceSessionRecord:
        async with self._lock:
            record = self._records.setdefault(session_id, VoiceSessionRecord(session_id))
            if (
                record.status is VoiceConnectionStatus.CONNECTING
                and record.reservation_expires_at is not None
                and now >= record.reservation_expires_at
            ):
                record.status = VoiceConnectionStatus.DISCONNECTED
                record.conversation_id = None
                record.reservation_expires_at = None
            if record.status in {
                VoiceConnectionStatus.CONNECTING,
                VoiceConnectionStatus.CONNECTED,
                VoiceConnectionStatus.RECONNECTING,
                VoiceConnectionStatus.RECONNECTED,
            }:
                raise VoiceConversationAlreadyActive(
                    "A voice conversation is already active for this session"
                )
            if record.retry_after is not None and now < record.retry_after:
                raise VoiceRetryLater(record.retry_after)
            record.status = VoiceConnectionStatus.CONNECTING
            record.conversation_id = None
            record.updated_at = now
            record.reservation_expires_at = now + self._reservation_ttl
            record.version += 1
            return copy.deepcopy(record)

    async def token_issued(
        self, session_id: str, conversation_id: str, *, now: datetime
    ) -> VoiceSessionRecord:
        async with self._lock:
            record = self._record(session_id)
            if record.status is not VoiceConnectionStatus.CONNECTING:
                raise VoiceConversationError("Voice access was not reserved")
            record.conversation_id = conversation_id
            record.updated_at = now
            record.version += 1
            return copy.deepcopy(record)

    async def update(
        self,
        session_id: str,
        status: VoiceConnectionStatus,
        *,
        now: datetime,
        conversation_id: str | None = None,
    ) -> VoiceSessionRecord:
        async with self._lock:
            record = self._records.setdefault(session_id, VoiceSessionRecord(session_id))
            if (
                record.conversation_id is not None
                and conversation_id is not None
                and record.conversation_id != conversation_id
            ):
                raise VoiceConversationError(
                    "conversation_id does not match the active voice session"
                )
            record.status = status
            if conversation_id is not None:
                record.conversation_id = conversation_id
            record.updated_at = now
            record.reservation_expires_at = None
            if status in {
                VoiceConnectionStatus.CONNECTED,
                VoiceConnectionStatus.RECONNECTED,
                VoiceConnectionStatus.DISCONNECTED,
            }:
                record.consecutive_failures = 0
                record.retry_after = None
            elif status in {
                VoiceConnectionStatus.API_ERROR,
                VoiceConnectionStatus.TIMEOUT,
                VoiceConnectionStatus.SERVICE_UNAVAILABLE,
            }:
                self._apply_backoff(record, now)
            record.version += 1
            return copy.deepcopy(record)

    async def provider_failed(
        self,
        session_id: str,
        *,
        now: datetime,
        retryable: bool,
    ) -> VoiceSessionRecord:
        status = (
            VoiceConnectionStatus.SERVICE_UNAVAILABLE
            if retryable
            else VoiceConnectionStatus.API_ERROR
        )
        return await self.update(session_id, status, now=now)

    async def get(self, session_id: str) -> VoiceSessionRecord:
        async with self._lock:
            return copy.deepcopy(
                self._records.get(session_id, VoiceSessionRecord(session_id=session_id))
            )

    def _record(self, session_id: str) -> VoiceSessionRecord:
        try:
            return self._records[session_id]
        except KeyError as exc:
            raise VoiceConversationError("Voice session is not reserved") from exc

    def _apply_backoff(self, record: VoiceSessionRecord, now: datetime) -> None:
        record.consecutive_failures += 1
        multiplier = 2 ** (record.consecutive_failures - 1)
        delay = min(self._retry_base * multiplier, self._retry_cap)
        record.retry_after = now + delay
