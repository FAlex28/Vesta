from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.safety import (
    AlertReason,
    AudioStatus,
    CancellationReason,
    CheckInStatus,
    ConnectivityStatus,
    GpsStatus,
    HealthStatus,
    InvalidCheckIn,
    InvalidTransition,
    PageVisibility,
    PowerStatus,
    SafetyEngine,
    SafetyPolicy,
    SafetySession,
    SessionState,
    TechnicalIssue,
    TransitionKind,
)

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)


@pytest.fixture
def policy() -> SafetyPolicy:
    return SafetyPolicy(
        check_in_interval=timedelta(seconds=60),
        response_timeout=timedelta(seconds=10),
        retry_delay=timedelta(seconds=5),
        technical_issue_retry_delay=timedelta(seconds=3),
        quick_cancel_window=timedelta(seconds=5),
        max_attempts=2,
    )


@pytest.fixture
def engine(policy: SafetyPolicy) -> SafetyEngine:
    ids = iter(f"checkin-{number}" for number in range(1, 100))
    return SafetyEngine(policy, checkin_id_factory=lambda: next(ids))


@pytest.fixture
def session() -> SafetySession:
    return SafetySession(session_id="session-1", device_id="vesta-01")


def activate(engine: SafetyEngine, session: SafetySession) -> None:
    engine.activate(session, now=NOW)


def start_check_in(engine: SafetyEngine, session: SafetySession) -> str:
    activate(engine, session)
    engine.advance(session, now=NOW + timedelta(seconds=60))
    assert session.current_checkin_id is not None
    return session.current_checkin_id


def deliver(
    engine: SafetyEngine,
    session: SafetySession,
    checkin_id: str,
    *,
    at: datetime,
) -> None:
    engine.mark_checkin_delivery(session, checkin_id, delivered=True, now=at)


def test_exactly_six_safety_states_are_preserved() -> None:
    assert list(SessionState) == [
        SessionState.IDLE,
        SessionState.ACTIVE,
        SessionState.CHECK_IN,
        SessionState.UNCONFIRMED,
        SessionState.ALERT,
        SessionState.CANCELLED,
    ]


def test_activate_initializes_quick_cancel_and_version(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    transition = engine.activate(session, now=NOW)

    assert transition.kind is TransitionKind.ACTIVATED
    assert transition.version == transition.details["version"] == session.version == 1
    assert session.next_check_in_at == NOW + timedelta(seconds=60)
    assert session.quick_cancel_until == NOW + timedelta(seconds=5)


def test_activation_is_not_repeatable(engine: SafetyEngine, session: SafetySession) -> None:
    activate(engine, session)
    with pytest.raises(InvalidTransition, match="Cannot activate"):
        engine.activate(session, now=NOW)


def test_check_in_starts_with_injected_unique_id(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    activate(engine, session)
    before = engine.advance(session, now=NOW + timedelta(seconds=59))
    started = engine.advance(session, now=NOW + timedelta(seconds=60))

    assert before.kind is TransitionKind.NO_CHANGE
    assert started.kind is TransitionKind.CHECK_IN_STARTED
    assert started.checkin_id == started.details["checkin_id"] == "checkin-1"
    assert session.current_checkin_status is CheckInStatus.REQUESTED
    assert session.response_deadline == NOW + timedelta(seconds=70)


def test_factory_duplicates_are_skipped_between_retries(
    policy: SafetyPolicy,
    session: SafetySession,
) -> None:
    ids = iter(["same-id", "same-id", "different-id"])
    engine = SafetyEngine(policy, checkin_id_factory=lambda: next(ids))
    first = start_check_in(engine, session)
    engine.advance(session, now=NOW + timedelta(seconds=70))
    retried = engine.advance(session, now=NOW + timedelta(seconds=75))

    assert first == "same-id"
    assert retried.checkin_id == "different-id"


def test_delivery_requires_active_matching_id_without_mutation(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    checkin_id = start_check_in(engine, session)
    version = session.version
    with pytest.raises(InvalidCheckIn, match="does not match"):
        engine.mark_checkin_delivery(
            session,
            "stale-id",
            delivered=True,
            now=NOW + timedelta(seconds=61),
        )
    assert session.current_checkin_id == checkin_id
    assert session.version == version


def test_positive_delivery_is_versioned(engine: SafetyEngine, session: SafetySession) -> None:
    checkin_id = start_check_in(engine, session)
    transition = engine.mark_checkin_delivered(
        session,
        checkin_id,
        now=NOW + timedelta(seconds=61),
    )

    assert transition.kind is TransitionKind.CHECK_IN_DELIVERED
    assert transition.version == session.version == 3
    assert session.current_checkin_status is CheckInStatus.DELIVERED
    assert session.current_checkin_delivered_at == NOW + timedelta(seconds=61)


def test_negative_delivery_stays_active_for_web_confirmation(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    checkin_id = start_check_in(engine, session)
    undelivered = engine.mark_checkin_delivery(
        session,
        checkin_id,
        delivered=False,
        now=NOW + timedelta(seconds=61),
    )
    assert undelivered.kind is TransitionKind.CHECK_IN_UNDELIVERED
    assert session.state is SessionState.CHECK_IN
    assert session.current_checkin_status is CheckInStatus.UNDELIVERED

    confirmed = engine.confirm_safe(session, checkin_id, now=NOW + timedelta(seconds=62))
    assert confirmed.kind is TransitionKind.CHECK_IN_CONFIRMED
    assert session.state is SessionState.ACTIVE
    assert session.last_checkin_id == checkin_id
    assert session.last_checkin_status is CheckInStatus.CONFIRMED


def test_duplicate_delivery_result_is_idempotent(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    checkin_id = start_check_in(engine, session)
    engine.mark_checkin_delivery(
        session,
        checkin_id,
        delivered=False,
        now=NOW + timedelta(seconds=61),
    )
    version = session.version
    duplicate = engine.mark_checkin_delivery(
        session,
        checkin_id,
        delivered=False,
        now=NOW + timedelta(seconds=62),
    )
    assert duplicate.kind is TransitionKind.NO_CHANGE
    assert session.version == version


def test_confirmation_before_delivery_ack_is_allowed(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    checkin_id = start_check_in(engine, session)
    transition = engine.confirm_safe(session, checkin_id, now=NOW + timedelta(seconds=61))
    assert transition.kind is TransitionKind.CHECK_IN_CONFIRMED
    assert session.last_checkin_status is CheckInStatus.CONFIRMED


def test_confirmation_requires_matching_id_without_mutation(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    checkin_id = start_check_in(engine, session)
    version = session.version
    with pytest.raises(InvalidCheckIn, match="does not match"):
        engine.confirm_safe(session, "another-id", now=NOW + timedelta(seconds=61))
    assert session.current_checkin_id == checkin_id
    assert session.version == version


@pytest.mark.parametrize("seconds", [70, 71])
def test_late_confirmation_is_rejected_before_scheduler_runs(
    engine: SafetyEngine,
    session: SafetySession,
    seconds: int,
) -> None:
    checkin_id = start_check_in(engine, session)
    version = session.version
    with pytest.raises(InvalidCheckIn, match="expired"):
        engine.confirm_safe(session, checkin_id, now=NOW + timedelta(seconds=seconds))
    assert session.state is SessionState.CHECK_IN
    assert session.version == version


def test_requested_timeout_retries_without_false_miss(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    checkin_id = start_check_in(engine, session)
    transition = engine.advance(session, now=NOW + timedelta(seconds=70))

    assert transition.kind is TransitionKind.CHECK_IN_UNDELIVERED
    assert transition.checkin_id == checkin_id
    assert session.state is SessionState.UNCONFIRMED
    assert session.last_checkin_status is CheckInStatus.UNDELIVERED
    assert session.missed_attempts == 0
    assert session.retry_at == NOW + timedelta(seconds=75)


def test_repeated_undelivered_requests_never_alert(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    start_check_in(engine, session)
    engine.advance(session, now=NOW + timedelta(seconds=70))
    engine.advance(session, now=NOW + timedelta(seconds=75))
    engine.advance(session, now=NOW + timedelta(seconds=85))
    third = engine.advance(session, now=NOW + timedelta(seconds=90))

    assert third.kind is TransitionKind.CHECK_IN_STARTED
    assert session.missed_attempts == 0
    assert session.undelivered_attempts == 2
    assert session.current_attempt == 1
    assert session.alert_reason is None


def test_only_delivered_timeout_counts_as_missed(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    checkin_id = start_check_in(engine, session)
    deliver(engine, session, checkin_id, at=NOW + timedelta(seconds=61))
    transition = engine.advance(session, now=NOW + timedelta(seconds=70))

    assert transition.kind is TransitionKind.CHECK_IN_MISSED
    assert transition.details["status"] == CheckInStatus.TIMED_OUT.value
    assert session.last_checkin_status is CheckInStatus.TIMED_OUT
    assert session.missed_attempts == 1


def test_two_delivered_timeouts_alert(engine: SafetyEngine, session: SafetySession) -> None:
    first = start_check_in(engine, session)
    deliver(engine, session, first, at=NOW + timedelta(seconds=61))
    engine.advance(session, now=NOW + timedelta(seconds=70))
    retry = engine.advance(session, now=NOW + timedelta(seconds=75))
    assert retry.checkin_id is not None
    deliver(engine, session, retry.checkin_id, at=NOW + timedelta(seconds=76))
    transition = engine.advance(session, now=NOW + timedelta(seconds=85))

    assert transition.kind is TransitionKind.ALERTED
    assert transition.details["reason"] == AlertReason.MISSED_CHECK_INS.value
    assert session.state is SessionState.ALERT
    assert session.missed_attempts == 2


@pytest.mark.parametrize(
    "issue",
    [
        TechnicalIssue.VOICE_UNAVAILABLE,
        TechnicalIssue.VOICE_TIMEOUT,
        TechnicalIssue.MICROPHONE_DENIED,
        TechnicalIssue.AUDIO_INTERRUPTED,
        TechnicalIssue.AUDIO_DISCONNECTED,
    ],
)
def test_voice_mic_and_audio_failures_still_create_web_fallback_checkin(
    engine: SafetyEngine,
    session: SafetySession,
    issue: TechnicalIssue,
) -> None:
    activate(engine, session)
    engine.report_technical_issue(session, issue, now=NOW + timedelta(seconds=55))
    started = engine.advance(session, now=NOW + timedelta(seconds=60))

    assert started.kind is TransitionKind.CHECK_IN_STARTED
    assert started.checkin_id is not None
    engine.mark_checkin_delivery(
        session,
        started.checkin_id,
        delivered=False,
        now=NOW + timedelta(seconds=61),
    )
    confirmed = engine.confirm_safe(
        session,
        started.checkin_id,
        now=NOW + timedelta(seconds=62),
    )
    assert confirmed.kind is TransitionKind.CHECK_IN_CONFIRMED


def test_network_disconnection_defers_request_creation(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    activate(engine, session)
    engine.report_technical_issue(
        session,
        TechnicalIssue.NETWORK_DISCONNECTED,
        now=NOW + timedelta(seconds=55),
    )
    transition = engine.advance(session, now=NOW + timedelta(seconds=60))

    assert transition.kind is TransitionKind.CHECK_IN_DEFERRED
    assert session.state is SessionState.ACTIVE
    assert session.next_check_in_at == NOW + timedelta(seconds=63)


@pytest.mark.parametrize(
    "issue",
    [
        TechnicalIssue.GPS_STALE,
        TechnicalIssue.GPS_UNAVAILABLE,
        TechnicalIssue.PAGE_HIDDEN,
        TechnicalIssue.LOW_VOLTAGE,
        TechnicalIssue.POWER_UNSTABLE,
        TechnicalIssue.PROCESS_RESTARTED,
    ],
)
def test_degrading_issues_never_block_checkin_creation(
    engine: SafetyEngine,
    session: SafetySession,
    issue: TechnicalIssue,
) -> None:
    activate(engine, session)
    engine.report_technical_issue(session, issue, now=NOW + timedelta(seconds=55))
    transition = engine.advance(session, now=NOW + timedelta(seconds=60))

    assert transition.kind is TransitionKind.CHECK_IN_STARTED
    assert session.state is SessionState.CHECK_IN
    assert session.health_status is HealthStatus.DEGRADED


def test_delivered_timeout_is_deferred_while_channel_is_broken(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    checkin_id = start_check_in(engine, session)
    deliver(engine, session, checkin_id, at=NOW + timedelta(seconds=61))
    engine.report_technical_issue(
        session,
        TechnicalIssue.VOICE_UNAVAILABLE,
        now=NOW + timedelta(seconds=69),
    )
    transition = engine.advance(session, now=NOW + timedelta(seconds=70))

    assert transition.kind is TransitionKind.CHECK_IN_DEFERRED
    assert session.missed_attempts == 0
    assert session.response_deadline == NOW + timedelta(seconds=73)


def test_health_is_calculated_independently_from_safety_state() -> None:
    session = SafetySession(session_id="health", device_id="vesta-01")
    assert session.gps_status is GpsStatus.GPS_UNAVAILABLE
    assert session.health_status is HealthStatus.DEGRADED

    session.gps_status = GpsStatus.GPS_AVAILABLE
    assert session.health_status is HealthStatus.HEALTHY

    session.technical_issues.add(TechnicalIssue.PROCESS_RESTARTED)
    assert session.health_status is HealthStatus.DEGRADED
    assert session.state is SessionState.IDLE

    session.technical_issues.add(TechnicalIssue.NETWORK_DISCONNECTED)
    assert session.health_status is HealthStatus.DISCONNECTED
    assert session.state is SessionState.IDLE


def test_client_status_axes_are_orthogonal_to_safety_state() -> None:
    session = SafetySession(session_id="axes", device_id="vesta-01")
    session.page_visibility = PageVisibility.HIDDEN
    session.connectivity_status = ConnectivityStatus.RECONNECTING
    session.audio_status = AudioStatus.ERROR
    session.gps_status = GpsStatus.GPS_STALE
    session.power_status = PowerStatus.LOW_VOLTAGE

    assert session.state is SessionState.IDLE
    assert session.health_status is HealthStatus.DEGRADED

    session.connectivity_status = ConnectivityStatus.DISCONNECTED
    assert session.health_status is HealthStatus.DISCONNECTED


def test_duplicate_technical_report_is_noop_without_version_increment(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    activate(engine, session)
    engine.report_technical_issue(
        session,
        TechnicalIssue.PROCESS_RESTARTED,
        now=NOW + timedelta(seconds=1),
    )
    version = session.version
    duplicate = engine.report_technical_issue(
        session,
        TechnicalIssue.PROCESS_RESTARTED,
        now=NOW + timedelta(seconds=2),
    )

    assert duplicate.kind is TransitionKind.NO_CHANGE
    assert session.version == version


def test_resolving_issue_updates_health_and_version(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    session.gps_status = GpsStatus.GPS_AVAILABLE
    activate(engine, session)
    engine.report_technical_issue(
        session,
        TechnicalIssue.NETWORK_DISCONNECTED,
        now=NOW + timedelta(seconds=1),
    )
    version = session.version
    transition = engine.resolve_technical_issue(
        session,
        TechnicalIssue.NETWORK_DISCONNECTED,
        now=NOW + timedelta(seconds=2),
    )

    assert transition.kind is TransitionKind.TECHNICAL_ISSUE_RESOLVED
    assert transition.details["health_status"] == HealthStatus.HEALTHY.value
    assert session.version == version + 1


def test_quick_cancel_uses_accidental_activation_reason(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    activate(engine, session)
    transition = engine.cancel(session, now=NOW + timedelta(seconds=5))

    assert transition.details["quick_cancel"] is True
    assert session.cancellation_reason == CancellationReason.ACCIDENTAL_ACTIVATION.value


def test_regular_cancel_records_explicit_reason(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    activate(engine, session)
    engine.cancel(
        session,
        now=NOW + timedelta(seconds=6),
        reason=CancellationReason.SESSION_ENDED,
    )
    assert session.cancellation_reason == CancellationReason.SESSION_ENDED.value


@pytest.mark.parametrize(
    "starting_state",
    [SessionState.ACTIVE, SessionState.CHECK_IN, SessionState.UNCONFIRMED],
)
def test_cancel_is_limited_to_non_alerting_active_states(
    engine: SafetyEngine,
    session: SafetySession,
    starting_state: SessionState,
) -> None:
    activate(engine, session)
    session.state = starting_state
    transition = engine.cancel(
        session,
        now=NOW + timedelta(seconds=6),
        reason="operator_cancelled",
    )
    assert transition.to_state is SessionState.CANCELLED
    assert session.cancellation_reason == "operator_cancelled"


def test_alert_requires_explicit_resolution(engine: SafetyEngine, session: SafetySession) -> None:
    activate(engine, session)
    engine.request_sos(session, now=NOW + timedelta(seconds=1))
    version = session.version

    with pytest.raises(InvalidTransition, match="Cannot cancel"):
        engine.cancel(session, now=NOW + timedelta(seconds=2))
    assert session.state is SessionState.ALERT
    assert session.version == version

    resolved = engine.resolve_alert(session, now=NOW + timedelta(seconds=3))
    assert resolved.kind is TransitionKind.ALERT_RESOLVED
    assert resolved.from_state is SessionState.ALERT
    assert resolved.to_state is SessionState.CANCELLED
    assert session.cancellation_reason == CancellationReason.ALERT_RESOLVED.value


@pytest.mark.parametrize(
    "starting_state",
    [SessionState.ACTIVE, SessionState.CHECK_IN, SessionState.UNCONFIRMED],
)
def test_sos_alerts_immediately_with_spec_reason(
    engine: SafetyEngine,
    session: SafetySession,
    starting_state: SessionState,
) -> None:
    activate(engine, session)
    session.state = starting_state
    transition = engine.request_sos(session, now=NOW + timedelta(seconds=1))

    assert transition.details["reason"] == "SOS_EXPLICIT"
    assert session.alert_reason is AlertReason.SOS_EXPLICIT
    assert AlertReason.EXPLICIT_SOS is AlertReason.SOS_EXPLICIT


def test_confirmation_in_alert_is_rejected_without_mutation(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    checkin_id = start_check_in(engine, session)
    engine.request_sos(session, now=NOW + timedelta(seconds=61))
    version = session.version
    with pytest.raises(InvalidTransition, match="Cannot confirm safety"):
        engine.confirm_safe(session, checkin_id, now=NOW + timedelta(seconds=62))
    assert session.state is SessionState.ALERT
    assert session.version == version


def test_every_mutation_increments_version_and_noops_do_not(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    engine.activate(session, now=NOW)
    assert session.version == 1
    engine.advance(session, now=NOW + timedelta(seconds=1))
    assert session.version == 1
    engine.report_technical_issue(
        session,
        TechnicalIssue.PAGE_HIDDEN,
        now=NOW + timedelta(seconds=2),
    )
    assert session.version == 2
    engine.resolve_technical_issue(
        session,
        TechnicalIssue.PAGE_HIDDEN,
        now=NOW + timedelta(seconds=3),
    )
    assert session.version == 3
    transition = engine.advance(session, now=NOW + timedelta(seconds=60))
    assert transition.version == transition.details["version"] == session.version == 4


def test_cancelled_state_is_terminal(engine: SafetyEngine, session: SafetySession) -> None:
    activate(engine, session)
    engine.cancel(session, now=NOW + timedelta(seconds=6))
    assert engine.advance(session, now=NOW + timedelta(days=1)).kind is TransitionKind.NO_CHANGE
    with pytest.raises(InvalidTransition):
        engine.request_sos(session, now=NOW + timedelta(days=1))
    with pytest.raises(InvalidTransition):
        engine.cancel(session, now=NOW + timedelta(days=1))
    with pytest.raises(InvalidTransition):
        engine.resolve_alert(session, now=NOW + timedelta(days=1))


def test_late_scheduler_gives_full_response_window(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    activate(engine, session)
    late = NOW + timedelta(minutes=10)
    engine.advance(session, now=late)
    assert session.response_deadline == late + timedelta(seconds=10)


def test_naive_datetime_is_rejected(engine: SafetyEngine, session: SafetySession) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        engine.activate(session, now=datetime(2026, 11, 14, 10, 0))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("check_in_interval", timedelta(0)),
        ("response_timeout", timedelta(seconds=-1)),
        ("retry_delay", timedelta(0)),
        ("technical_issue_retry_delay", timedelta(0)),
    ],
)
def test_policy_rejects_non_positive_durations(field: str, value: timedelta) -> None:
    with pytest.raises(ValueError, match=field):
        SafetyPolicy(**{field: value})


def test_policy_quick_cancel_defaults_to_five_seconds_and_rejects_negative() -> None:
    assert SafetyPolicy().quick_cancel_window == timedelta(seconds=5)
    with pytest.raises(ValueError, match="quick_cancel_window"):
        SafetyPolicy(quick_cancel_window=timedelta(microseconds=-1))


def test_policy_requires_retry_after_first_attempt() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        SafetyPolicy(max_attempts=1)
