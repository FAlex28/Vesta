"""Data types shared by the deterministic Vesta safety engine.

The voice model is deliberately absent from this module.  It may report an
explicit user action to the application, but it cannot mutate these states.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


class SessionState(StrEnum):
    """The six states required by the Vesta specification."""

    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    CHECK_IN = "CHECK_IN"
    UNCONFIRMED = "UNCONFIRMED"
    ALERT = "ALERT"
    CANCELLED = "CANCELLED"


class TechnicalIssue(StrEnum):
    """Technical conditions tracked independently from safety state."""

    NETWORK_DISCONNECTED = "network_disconnected"
    VOICE_UNAVAILABLE = "voice_unavailable"
    MICROPHONE_DENIED = "microphone_denied"
    AUDIO_INTERRUPTED = "audio_interrupted"


class AlertReason(StrEnum):
    """Only deterministic application rules can produce these reasons."""

    MISSED_CHECK_INS = "missed_check_ins"
    EXPLICIT_SOS = "explicit_sos"


class TransitionKind(StrEnum):
    """Semantic outcome returned by every state-machine operation."""

    ACTIVATED = "activated"
    CHECK_IN_STARTED = "check_in_started"
    CHECK_IN_MISSED = "check_in_missed"
    CHECK_IN_CONFIRMED = "check_in_confirmed"
    CHECK_IN_DEFERRED = "check_in_deferred"
    ALERTED = "alerted"
    CANCELLED = "cancelled"
    TECHNICAL_ISSUE_REPORTED = "technical_issue_reported"
    TECHNICAL_ISSUE_RESOLVED = "technical_issue_resolved"
    NO_CHANGE = "no_change"


@dataclass(frozen=True, slots=True)
class SafetyPolicy:
    """Timing and escalation policy for one Vesta process."""

    check_in_interval: timedelta = timedelta(seconds=60)
    response_timeout: timedelta = timedelta(seconds=15)
    retry_delay: timedelta = timedelta(seconds=5)
    technical_issue_retry_delay: timedelta = timedelta(seconds=5)
    max_attempts: int = 2

    def __post_init__(self) -> None:
        durations = {
            "check_in_interval": self.check_in_interval,
            "response_timeout": self.response_timeout,
            "retry_delay": self.retry_delay,
            "technical_issue_retry_delay": self.technical_issue_retry_delay,
        }
        for name, value in durations.items():
            if value.total_seconds() <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.max_attempts < 2:
            raise ValueError("max_attempts must be at least two so a retry is always allowed")


@dataclass(slots=True)
class SafetySession:
    """Mutable aggregate controlled exclusively by :class:`SafetyEngine`."""

    session_id: str
    device_id: str
    state: SessionState = SessionState.IDLE
    created_at: datetime | None = None
    activated_at: datetime | None = None
    updated_at: datetime | None = None
    next_check_in_at: datetime | None = None
    response_deadline: datetime | None = None
    retry_at: datetime | None = None
    current_attempt: int = 0
    missed_attempts: int = 0
    technical_issues: set[TechnicalIssue] = field(default_factory=set)
    alert_reason: AlertReason | None = None
    cancelled_at: datetime | None = None
    client_last_seen_at: datetime | None = None
    connection_lease_expires_at: datetime | None = None

    @property
    def has_blocking_technical_issue(self) -> bool:
        """Whether automatic safety escalation must currently be suspended."""

        return bool(self.technical_issues)

    @property
    def is_terminal(self) -> bool:
        return self.state is SessionState.CANCELLED


@dataclass(frozen=True, slots=True)
class SafetyTransition:
    """An auditable description of an engine operation."""

    kind: TransitionKind
    from_state: SessionState
    to_state: SessionState
    occurred_at: datetime
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def state_changed(self) -> bool:
        return self.from_state is not self.to_state
