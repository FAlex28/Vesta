from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.alerts import (
    AlertIdCollision,
    AlertIncidentType,
    AlertNotFound,
    AlertRegistry,
    AlertSessionMismatch,
    AlertStatus,
    AlertTimestampError,
)

NOW = datetime(2026, 11, 14, 16, 30, tzinfo=UTC)


def registry() -> AlertRegistry:
    return AlertRegistry(id_factory=lambda: "alert-001")


def test_first_incident_creates_active_alert_and_requests_notification_once() -> None:
    alerts = registry()

    alert, created = alerts.ensure_active(
        "session-001",
        AlertIncidentType.MISSED_CHECK_INS,
        now=NOW,
    )

    assert created is True
    assert alert.alert_id == "alert-001"
    assert alert.session_id == "session-001"
    assert alert.status is AlertStatus.ACTIVE
    assert alert.incident_type is AlertIncidentType.MISSED_CHECK_INS
    assert alert.created_at == NOW
    assert alert.updated_at == NOW
    assert alert.notification_requested_at == NOW
    assert alert.resolved_at is None
    assert alert.resolution_action is None
    assert alert.version == 1


def test_replay_returns_same_alert_without_new_version_or_notification() -> None:
    alerts = registry()
    first, _ = alerts.ensure_active("session-001", AlertIncidentType.EXPLICIT_SOS, now=NOW)

    replay, created = alerts.ensure_active(
        "session-001",
        AlertIncidentType.EXPLICIT_SOS,
        now=NOW + timedelta(seconds=10),
    )

    assert created is False
    assert replay == first
    assert replay.version == 1
    assert replay.notification_requested_at == NOW


def test_sos_and_timeout_share_one_alert_and_sos_has_priority() -> None:
    alerts = registry()
    missed, created = alerts.ensure_active(
        "session-001", AlertIncidentType.MISSED_CHECK_INS, now=NOW
    )

    sos, created_again = alerts.ensure_active(
        "session-001",
        AlertIncidentType.EXPLICIT_SOS,
        now=NOW + timedelta(milliseconds=1),
    )
    timeout_replay, timeout_created = alerts.ensure_active(
        "session-001",
        AlertIncidentType.MISSED_CHECK_INS,
        now=NOW + timedelta(seconds=1),
    )

    assert created is True
    assert created_again is False
    assert timeout_created is False
    assert sos.alert_id == missed.alert_id == timeout_replay.alert_id
    assert sos.incident_type is AlertIncidentType.EXPLICIT_SOS
    assert timeout_replay.incident_type is AlertIncidentType.EXPLICIT_SOS
    assert sos.version == 2
    assert timeout_replay.notification_requested_at == NOW
    assert len(alerts.list()) == 1


def test_explicit_sos_is_never_downgraded_by_timeout() -> None:
    alerts = registry()
    original, _ = alerts.ensure_active("session-001", AlertIncidentType.EXPLICIT_SOS, now=NOW)

    replay, created = alerts.ensure_active(
        "session-001",
        AlertIncidentType.MISSED_CHECK_INS,
        now=NOW + timedelta(seconds=1),
    )

    assert created is False
    assert replay == original
    assert replay.incident_type is AlertIncidentType.EXPLICIT_SOS


def test_resolution_is_explicit_idempotent_and_never_reactivated() -> None:
    alerts = registry()
    active, _ = alerts.ensure_active("session-001", AlertIncidentType.MISSED_CHECK_INS, now=NOW)

    resolved, changed = alerts.resolve(
        "session-001",
        active.alert_id,
        now=NOW + timedelta(seconds=5),
        action="USER_RESOLVED",
    )
    repeated, repeated_changed = alerts.resolve(
        "session-001",
        active.alert_id,
        now=NOW + timedelta(seconds=10),
        action="SHOULD_NOT_REWRITE",
    )
    stale_event, recreated = alerts.ensure_active(
        "session-001",
        AlertIncidentType.EXPLICIT_SOS,
        now=NOW + timedelta(seconds=20),
    )

    assert changed is True
    assert resolved.status is AlertStatus.RESOLVED
    assert resolved.resolved_at == NOW + timedelta(seconds=5)
    assert resolved.resolution_action == "USER_RESOLVED"
    assert resolved.version == 2
    assert repeated_changed is False
    assert repeated == resolved
    assert recreated is False
    assert stale_event == resolved


def test_lookup_errors_distinguish_missing_alert_and_session_mismatch() -> None:
    alerts = registry()
    alert, _ = alerts.ensure_active("session-001", AlertIncidentType.EXPLICIT_SOS, now=NOW)

    with pytest.raises(AlertNotFound):
        alerts.get("does-not-exist")
    with pytest.raises(AlertSessionMismatch):
        alerts.resolve(
            "session-002",
            alert.alert_id,
            now=NOW + timedelta(seconds=1),
            action="USER_RESOLVED",
        )


def test_all_read_apis_return_defensive_copies() -> None:
    alerts = registry()
    returned, _ = alerts.ensure_active("session-001", AlertIncidentType.EXPLICIT_SOS, now=NOW)
    returned.resolution_action = "tampered"
    returned.version = 999

    by_id = alerts.get("alert-001")
    by_session = alerts.get_for_session("session-001")
    listed = alerts.list()
    assert by_session is not None
    by_id.resolution_action = "another mutation"
    by_session.version = 777
    listed[0].status = AlertStatus.RESOLVED

    pristine = alerts.get("alert-001")
    assert pristine.resolution_action is None
    assert pristine.version == 1
    assert pristine.status is AlertStatus.ACTIVE


def test_timestamps_must_be_aware_and_monotonic() -> None:
    alerts = registry()
    with pytest.raises(ValueError, match="timezone-aware"):
        alerts.ensure_active(
            "session-001",
            AlertIncidentType.EXPLICIT_SOS,
            now=datetime(2026, 11, 14, 16, 30),
        )

    active, _ = alerts.ensure_active("session-001", AlertIncidentType.MISSED_CHECK_INS, now=NOW)
    with pytest.raises(AlertTimestampError):
        alerts.resolve(
            "session-001",
            active.alert_id,
            now=NOW - timedelta(seconds=1),
            action="USER_RESOLVED",
        )


def test_identifier_collisions_are_rejected() -> None:
    alerts = AlertRegistry(id_factory=lambda: "same-alert-id")
    alerts.ensure_active("session-001", AlertIncidentType.EXPLICIT_SOS, now=NOW)

    with pytest.raises(AlertIdCollision):
        alerts.ensure_active(
            "session-002",
            AlertIncidentType.MISSED_CHECK_INS,
            now=NOW + timedelta(seconds=1),
        )
