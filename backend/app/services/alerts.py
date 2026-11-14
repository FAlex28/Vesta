"""In-memory alert lifecycle registry for the Vesta safety coordinator.

The registry is deliberately synchronous.  ``SessionManager`` owns the
application-level lock and must serialize calls that mutate this registry.
Persistence and notification delivery remain integration-layer concerns.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class AlertStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"


class AlertIncidentType(StrEnum):
    EXPLICIT_SOS = "EXPLICIT_SOS"
    MISSED_CHECK_INS = "MISSED_CHECK_INS"


class AlertRegistryError(ValueError):
    """Base class for alert-registry failures."""


class AlertNotFound(AlertRegistryError):
    """No alert exists for the requested identifier."""


class AlertSessionMismatch(AlertRegistryError):
    """The alert belongs to a different Vesta session."""


class AlertTimestampError(AlertRegistryError):
    """An alert mutation attempted to move its clock backwards."""


class AlertIdCollision(AlertRegistryError):
    """The configured identifier factory returned an existing alert ID."""


@dataclass(slots=True)
class AlertRecord:
    alert_id: str
    session_id: str
    incident_type: AlertIncidentType
    status: AlertStatus
    created_at: datetime
    updated_at: datetime
    notification_requested_at: datetime
    version: int = 1
    resolved_at: datetime | None = None
    resolution_action: str | None = None
    # First time an authorized trusted contact displayed this alert. Creating an
    # alert, or requesting its notification, is never treated as "seen".
    viewed_at: datetime | None = None


AlertIdFactory = Callable[[], str]


class AlertRegistry:
    """Keep one immutable-in-history logical alert per Vesta session.

    Replayed commands return the existing record.  An explicit SOS upgrades an
    existing missed-check-in alert without creating a second notification.  A
    resolved alert is retained and can never be reactivated by an old event.
    """

    def __init__(self, *, id_factory: AlertIdFactory | None = None) -> None:
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._alerts_by_id: dict[str, AlertRecord] = {}
        self._alert_id_by_session: dict[str, str] = {}

    def ensure_active(
        self,
        session_id: str,
        incident_type: AlertIncidentType | str,
        *,
        now: datetime,
    ) -> tuple[AlertRecord, bool]:
        """Return the session's logical alert, creating it exactly once.

        ``created`` is true only for the first call.  A later explicit SOS has
        higher semantic priority than a missed-check-in reason, but it updates
        the same alert and does not request a second notification.
        """

        checked_session_id = _validate_identifier(session_id, "session_id")
        checked_incident = AlertIncidentType(incident_type)
        instant = _utc(now)
        existing_id = self._alert_id_by_session.get(checked_session_id)
        if existing_id is not None:
            record = self._alerts_by_id[existing_id]
            if record.status is AlertStatus.RESOLVED:
                return _clone(record), False
            if (
                checked_incident is AlertIncidentType.EXPLICIT_SOS
                and record.incident_type is AlertIncidentType.MISSED_CHECK_INS
            ):
                _require_not_before(instant, record.updated_at)
                record.incident_type = AlertIncidentType.EXPLICIT_SOS
                record.updated_at = instant
                record.version += 1
            return _clone(record), False

        alert_id = _validate_identifier(self._id_factory(), "alert_id")
        if alert_id in self._alerts_by_id:
            raise AlertIdCollision("alert ID factory returned an existing identifier")
        record = AlertRecord(
            alert_id=alert_id,
            session_id=checked_session_id,
            incident_type=checked_incident,
            status=AlertStatus.ACTIVE,
            created_at=instant,
            updated_at=instant,
            notification_requested_at=instant,
        )
        self._alerts_by_id[alert_id] = record
        self._alert_id_by_session[checked_session_id] = alert_id
        return _clone(record), True

    def resolve(
        self,
        session_id: str,
        alert_id: str,
        *,
        now: datetime,
        action: str,
    ) -> tuple[AlertRecord, bool]:
        """Explicitly resolve and retain an alert.

        Repeating the same or another resolution command is idempotent and does
        not rewrite the original resolution audit fields.
        """

        checked_session_id = _validate_identifier(session_id, "session_id")
        record = self._get_record(alert_id)
        if record.session_id != checked_session_id:
            raise AlertSessionMismatch("alert does not belong to the requested session")
        if record.status is AlertStatus.RESOLVED:
            return _clone(record), False

        instant = _utc(now)
        _require_not_before(instant, record.updated_at)
        checked_action = _validate_action(action)
        record.status = AlertStatus.RESOLVED
        record.resolved_at = instant
        record.resolution_action = checked_action
        record.updated_at = instant
        record.version += 1
        return _clone(record), True

    def get(self, alert_id: str) -> AlertRecord:
        return _clone(self._get_record(alert_id))

    def mark_viewed(self, alert_id: str, *, now: datetime) -> tuple[AlertRecord, bool]:
        """Record the first display to the trusted contact; later views keep that time."""

        record = self._get_record(alert_id)
        if record.viewed_at is not None:
            return _clone(record), False
        record.viewed_at = _utc(now)
        return _clone(record), True

    def get_for_session(self, session_id: str) -> AlertRecord | None:
        checked_session_id = _validate_identifier(session_id, "session_id")
        alert_id = self._alert_id_by_session.get(checked_session_id)
        if alert_id is None:
            return None
        return _clone(self._alerts_by_id[alert_id])

    def list(self) -> list[AlertRecord]:
        return [_clone(record) for record in self._alerts_by_id.values()]

    def restore(self, record: AlertRecord) -> AlertRecord:
        """Restore an exact persistent alert identity during process startup."""

        restored = _clone(record)
        _validate_identifier(restored.alert_id, "alert_id")
        _validate_identifier(restored.session_id, "session_id")
        restored.incident_type = AlertIncidentType(restored.incident_type)
        restored.status = AlertStatus(restored.status)
        created_at = _utc(restored.created_at)
        updated_at = _utc(restored.updated_at)
        notification_at = _utc(restored.notification_requested_at)
        _require_not_before(updated_at, created_at)
        _require_not_before(notification_at, created_at)
        _require_not_before(updated_at, notification_at)
        if restored.resolved_at is not None:
            resolved_at = _utc(restored.resolved_at)
            _require_not_before(resolved_at, updated_at)
        if restored.status is AlertStatus.ACTIVE and (
            restored.resolved_at is not None or restored.resolution_action is not None
        ):
            raise AlertRegistryError("active restored alert cannot have resolution fields")
        if restored.status is AlertStatus.RESOLVED and (
            restored.resolved_at is None or not restored.resolution_action
        ):
            raise AlertRegistryError("resolved restored alert requires resolution audit fields")
        if restored.version < 1:
            raise AlertRegistryError("restored alert version must be positive")
        existing_id = self._alert_id_by_session.get(restored.session_id)
        if existing_id is not None and existing_id != restored.alert_id:
            raise AlertIdCollision("session already has a different alert")
        existing = self._alerts_by_id.get(restored.alert_id)
        if existing is not None and existing.session_id != restored.session_id:
            raise AlertIdCollision("alert ID belongs to a different session")
        self._alerts_by_id[restored.alert_id] = restored
        self._alert_id_by_session[restored.session_id] = restored.alert_id
        return _clone(restored)

    def _get_record(self, alert_id: str) -> AlertRecord:
        checked_alert_id = _validate_identifier(alert_id, "alert_id")
        try:
            return self._alerts_by_id[checked_alert_id]
        except KeyError as exc:
            raise AlertNotFound("alert not found") from exc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("alert timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _require_not_before(value: datetime, previous: datetime) -> None:
    if value < previous:
        raise AlertTimestampError("alert timestamp cannot move backwards")


def _validate_identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{name} must be a non-empty string of at most 256 characters")
    return value.strip()


def _validate_action(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128:
        raise ValueError("resolution action must be a non-empty string of at most 128 characters")
    return value.strip()


def _clone(record: AlertRecord) -> AlertRecord:
    return copy.deepcopy(record)
