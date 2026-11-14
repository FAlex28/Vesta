"""Data types shared by the deterministic Vesta safety engine.

The six safety states are intentionally kept separate from technical health.
Voice or AI components may report explicit actions and health signals, but they
do not decide safety transitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


class SessionState(StrEnum):
    """The six safety states required by the Vesta specification."""

    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    CHECK_IN = "CHECK_IN"
    UNCONFIRMED = "UNCONFIRMED"
    ALERT = "ALERT"
    CANCELLED = "CANCELLED"


class HealthStatus(StrEnum):
    """Overall technical health, calculated independently from safety state."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"


class PageVisibility(StrEnum):
    """Latest page-visibility signal reported by the client."""

    VISIBLE = "VISIBLE"
    HIDDEN = "HIDDEN"
    UNKNOWN = "UNKNOWN"


class ConnectivityStatus(StrEnum):
    """Latest network-connectivity signal reported by the client."""

    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    DISCONNECTED = "DISCONNECTED"
    UNKNOWN = "UNKNOWN"


class AudioStatus(StrEnum):
    """Latest audio-channel status reported by the client."""

    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class GpsStatus(StrEnum):
    """Latest GPS freshness/availability status."""

    GPS_AVAILABLE = "GPS_AVAILABLE"
    GPS_STALE = "GPS_STALE"
    GPS_UNAVAILABLE = "GPS_UNAVAILABLE"


class PowerStatus(StrEnum):
    """Latest power-supply status reported by the hardware layer."""

    POWER_OK = "POWER_OK"
    POWER_UNSTABLE = "POWER_UNSTABLE"
    LOW_VOLTAGE = "LOW_VOLTAGE"
    POWER_STATUS_UNKNOWN = "POWER_STATUS_UNKNOWN"


class TechnicalIssue(StrEnum):
    """Technical conditions tracked independently from safety state."""

    NETWORK_DISCONNECTED = "network_disconnected"
    VOICE_UNAVAILABLE = "voice_unavailable"
    VOICE_TIMEOUT = "voice_timeout"
    MICROPHONE_DENIED = "microphone_denied"
    AUDIO_INTERRUPTED = "audio_interrupted"
    AUDIO_DISCONNECTED = "audio_disconnected"
    GPS_STALE = "gps_stale"
    GPS_UNAVAILABLE = "gps_unavailable"
    PAGE_HIDDEN = "page_hidden"
    LOW_VOLTAGE = "low_voltage"
    POWER_UNSTABLE = "power_unstable"
    PROCESS_RESTARTED = "process_restarted"

    @property
    def blocks_check_in(self) -> bool:
        """Whether this issue prevents a check-in from being delivered safely."""

        return self in {
            TechnicalIssue.NETWORK_DISCONNECTED,
            TechnicalIssue.VOICE_UNAVAILABLE,
            TechnicalIssue.VOICE_TIMEOUT,
            TechnicalIssue.MICROPHONE_DENIED,
            TechnicalIssue.AUDIO_INTERRUPTED,
            TechnicalIssue.AUDIO_DISCONNECTED,
        }


class CheckInStatus(StrEnum):
    """Delivery lifecycle of a single, uniquely identified check-in."""

    REQUESTED = "REQUESTED"
    DELIVERED = "DELIVERED"
    UNDELIVERED = "UNDELIVERED"
    TIMED_OUT = "TIMED_OUT"
    CONFIRMED = "CONFIRMED"


class CancellationReason(StrEnum):
    """Canonical cancellation reasons accepted by the engine."""

    USER_CANCELLED = "USER_CANCELLED"
    ACCIDENTAL_ACTIVATION = "ACCIDENTAL_ACTIVATION"
    QUICK_CANCEL = "ACCIDENTAL_ACTIVATION"
    SESSION_ENDED = "SESSION_ENDED"
    ALERT_RESOLVED = "ALERT_RESOLVED"


class AlertReason(StrEnum):
    """Only deterministic application rules can produce these reasons."""

    MISSED_CHECK_INS = "MISSED_CHECK_INS"
    SOS_EXPLICIT = "SOS_EXPLICIT"
    EXPLICIT_SOS = "SOS_EXPLICIT"


class TransitionKind(StrEnum):
    """Semantic outcome returned by every state-machine operation."""

    ACTIVATED = "activated"
    CHECK_IN_STARTED = "check_in_started"
    CHECK_IN_DELIVERED = "check_in_delivered"
    CHECK_IN_UNDELIVERED = "check_in_undelivered"
    CHECK_IN_MISSED = "check_in_missed"
    CHECK_IN_CONFIRMED = "check_in_confirmed"
    CHECK_IN_DEFERRED = "check_in_deferred"
    ALERTED = "alerted"
    ALERT_RESOLVED = "alert_resolved"
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
    quick_cancel_window: timedelta = timedelta(seconds=5)
    max_attempts: int = 2

    def __post_init__(self) -> None:
        positive_durations = {
            "check_in_interval": self.check_in_interval,
            "response_timeout": self.response_timeout,
            "retry_delay": self.retry_delay,
            "technical_issue_retry_delay": self.technical_issue_retry_delay,
        }
        for name, value in positive_durations.items():
            if value.total_seconds() <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.quick_cancel_window.total_seconds() < 0:
            raise ValueError("quick_cancel_window must not be negative")
        if self.max_attempts < 2:
            raise ValueError("max_attempts must be at least two so a retry is always allowed")


@dataclass(slots=True)
class SafetySession:
    """Mutable aggregate controlled by :class:`SafetyEngine` and its manager."""

    session_id: str
    device_id: str
    state: SessionState = SessionState.IDLE
    version: int = 0
    created_at: datetime | None = None
    activated_at: datetime | None = None
    updated_at: datetime | None = None
    next_check_in_at: datetime | None = None
    response_deadline: datetime | None = None
    retry_at: datetime | None = None
    quick_cancel_until: datetime | None = None
    current_attempt: int = 0
    missed_attempts: int = 0
    undelivered_attempts: int = 0
    current_checkin_id: str | None = None
    current_checkin_status: CheckInStatus | None = None
    current_checkin_requested_at: datetime | None = None
    current_checkin_delivered_at: datetime | None = None
    last_checkin_id: str | None = None
    last_checkin_status: CheckInStatus | None = None
    last_checkin_requested_at: datetime | None = None
    last_checkin_delivered_at: datetime | None = None
    last_confirmation_at: datetime | None = None
    technical_issues: set[TechnicalIssue] = field(default_factory=set)
    page_visibility: PageVisibility = PageVisibility.UNKNOWN
    connectivity_status: ConnectivityStatus = ConnectivityStatus.UNKNOWN
    audio_status: AudioStatus = AudioStatus.UNKNOWN
    gps_status: GpsStatus = GpsStatus.GPS_UNAVAILABLE
    power_status: PowerStatus = PowerStatus.POWER_STATUS_UNKNOWN
    alert_reason: AlertReason | None = None
    cancelled_at: datetime | None = None
    cancellation_reason: str | None = None
    client_last_seen_at: datetime | None = None
    connection_lease_expires_at: datetime | None = None

    @property
    def checkin_id(self) -> str | None:
        """Compatibility alias for the active check-in identifier."""

        return self.current_checkin_id

    @property
    def checkin_status(self) -> CheckInStatus | None:
        """Compatibility alias for the active check-in status."""

        return self.current_checkin_status

    @property
    def has_blocking_technical_issue(self) -> bool:
        """Whether check-in delivery/escalation must currently be suspended."""

        return (
            any(issue.blocks_check_in for issue in self.technical_issues)
            or self.connectivity_status is ConnectivityStatus.DISCONNECTED
            or self.audio_status in {AudioStatus.DISCONNECTED, AudioStatus.ERROR}
        )

    @property
    def health_status(self) -> HealthStatus:
        """Calculate technical health without altering the safety state."""

        disconnected_issues = {
            TechnicalIssue.NETWORK_DISCONNECTED,
            TechnicalIssue.AUDIO_DISCONNECTED,
        }
        if (
            self.technical_issues.intersection(disconnected_issues)
            or self.connectivity_status is ConnectivityStatus.DISCONNECTED
            or self.audio_status is AudioStatus.DISCONNECTED
        ):
            return HealthStatus.DISCONNECTED

        degraded_status = (
            bool(self.technical_issues)
            or self.connectivity_status is ConnectivityStatus.RECONNECTING
            or self.audio_status is AudioStatus.ERROR
            or self.page_visibility is PageVisibility.HIDDEN
            or self.gps_status in {GpsStatus.GPS_STALE, GpsStatus.GPS_UNAVAILABLE}
            or self.power_status in {PowerStatus.POWER_UNSTABLE, PowerStatus.LOW_VOLTAGE}
        )
        return HealthStatus.DEGRADED if degraded_status else HealthStatus.HEALTHY

    def is_within_quick_cancel_window(self, now: datetime) -> bool:
        """Return whether ``now`` is still inside the inclusive quick-cancel window."""

        return self.quick_cancel_until is not None and now <= self.quick_cancel_until

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
    version: int
    checkin_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def state_changed(self) -> bool:
        return self.from_state is not self.to_state
