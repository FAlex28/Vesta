from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.safety import SafetySession
from app.services.alerts import AlertRecord


@dataclass(frozen=True, slots=True)
class RecoveredCommand:
    """Persisted idempotency result reconstructed by the integration role."""

    event_id: str
    fingerprint: str
    session_id: str


@dataclass(frozen=True, slots=True)
class RecoveredSessionBundle:
    """Full recovery aggregate; keeps alert identity across process restarts."""

    session: SafetySession
    alert: AlertRecord | None = None
    processed_commands: tuple[RecoveredCommand, ...] = ()


@runtime_checkable
class SessionRecoveryProvider(Protocol):
    """Boundary implemented by Agent 3's persistent session repository."""

    async def load_unfinished_sessions(
        self,
    ) -> list[SafetySession | RecoveredSessionBundle]:
        """Return unfinished sessions reconstructed from persistent data."""


class NoopSessionRecoveryProvider:
    """Default until the Tiger Data integration supplies persistent sessions."""

    async def load_unfinished_sessions(
        self,
    ) -> list[SafetySession | RecoveredSessionBundle]:
        return []


class StaticSessionRecoveryProvider:
    """Test adapter that never exposes its mutable source objects."""

    def __init__(self, sessions: list[SafetySession | RecoveredSessionBundle]) -> None:
        self._sessions = copy.deepcopy(sessions)

    async def load_unfinished_sessions(
        self,
    ) -> list[SafetySession | RecoveredSessionBundle]:
        return copy.deepcopy(self._sessions)
