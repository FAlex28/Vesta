from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.safety import (
    AlertReason,
    InvalidTransition,
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
        max_attempts=2,
    )


@pytest.fixture
def engine(policy: SafetyPolicy) -> SafetyEngine:
    return SafetyEngine(policy)


@pytest.fixture
def session() -> SafetySession:
    return SafetySession(session_id="session-1", device_id="vesta-01")


def activate(engine: SafetyEngine, session: SafetySession) -> None:
    engine.activate(session, now=NOW)


def start_check_in(engine: SafetyEngine, session: SafetySession) -> None:
    activate(engine, session)
    engine.advance(session, now=NOW + timedelta(seconds=60))


def test_activate_idle_session(engine: SafetyEngine, session: SafetySession) -> None:
    transition = engine.activate(session, now=NOW)

    assert transition.kind is TransitionKind.ACTIVATED
    assert transition.from_state is SessionState.IDLE
    assert transition.to_state is SessionState.ACTIVE
    assert session.next_check_in_at == NOW + timedelta(seconds=60)
    assert session.activated_at == NOW


def test_activation_is_not_repeatable(engine: SafetyEngine, session: SafetySession) -> None:
    activate(engine, session)

    with pytest.raises(InvalidTransition, match="Cannot activate"):
        engine.activate(session, now=NOW)


def test_check_in_starts_exactly_at_deadline(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    activate(engine, session)

    before = engine.advance(session, now=NOW + timedelta(seconds=59))
    started = engine.advance(session, now=NOW + timedelta(seconds=60))

    assert before.kind is TransitionKind.NO_CHANGE
    assert started.kind is TransitionKind.CHECK_IN_STARTED
    assert session.state is SessionState.CHECK_IN
    assert session.current_attempt == 1
    assert session.response_deadline == NOW + timedelta(seconds=70)


def test_confirmation_returns_to_active_and_resets_escalation(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    start_check_in(engine, session)

    transition = engine.confirm_safe(session, now=NOW + timedelta(seconds=65))

    assert transition.kind is TransitionKind.CHECK_IN_CONFIRMED
    assert session.state is SessionState.ACTIVE
    assert session.current_attempt == 0
    assert session.missed_attempts == 0
    assert session.next_check_in_at == NOW + timedelta(seconds=125)
    assert session.response_deadline is None


def test_first_timeout_enters_unconfirmed_then_starts_retry(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    start_check_in(engine, session)

    missed = engine.advance(session, now=NOW + timedelta(seconds=70))
    retry = engine.advance(session, now=NOW + timedelta(seconds=75))

    assert missed.kind is TransitionKind.CHECK_IN_MISSED
    assert missed.to_state is SessionState.UNCONFIRMED
    assert retry.kind is TransitionKind.CHECK_IN_STARTED
    assert retry.to_state is SessionState.CHECK_IN
    assert session.current_attempt == 2
    assert session.missed_attempts == 1


def test_confirmation_during_unconfirmed_prevents_alert(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    start_check_in(engine, session)
    engine.advance(session, now=NOW + timedelta(seconds=70))

    transition = engine.confirm_safe(session, now=NOW + timedelta(seconds=72))

    assert transition.from_state is SessionState.UNCONFIRMED
    assert session.state is SessionState.ACTIVE
    assert session.missed_attempts == 0
    assert session.alert_reason is None


def test_repeated_timeout_alerts_deterministically(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    start_check_in(engine, session)
    engine.advance(session, now=NOW + timedelta(seconds=70))
    engine.advance(session, now=NOW + timedelta(seconds=75))

    transition = engine.advance(session, now=NOW + timedelta(seconds=85))

    assert transition.kind is TransitionKind.ALERTED
    assert transition.details["reason"] == AlertReason.MISSED_CHECK_INS.value
    assert session.state is SessionState.ALERT
    assert session.alert_reason is AlertReason.MISSED_CHECK_INS
    assert session.missed_attempts == 2
    assert session.response_deadline is None


@pytest.mark.parametrize(
    "starting_state",
    [SessionState.ACTIVE, SessionState.CHECK_IN, SessionState.UNCONFIRMED],
)
def test_sos_alerts_immediately_from_active_states(
    engine: SafetyEngine,
    session: SafetySession,
    starting_state: SessionState,
) -> None:
    activate(engine, session)
    session.state = starting_state

    transition = engine.request_sos(session, now=NOW + timedelta(seconds=1))

    assert transition.kind is TransitionKind.ALERTED
    assert session.state is SessionState.ALERT
    assert session.alert_reason is AlertReason.EXPLICIT_SOS


@pytest.mark.parametrize(
    "starting_state",
    [
        SessionState.ACTIVE,
        SessionState.CHECK_IN,
        SessionState.UNCONFIRMED,
        SessionState.ALERT,
    ],
)
def test_authorized_layer_can_cancel_supported_states(
    engine: SafetyEngine,
    session: SafetySession,
    starting_state: SessionState,
) -> None:
    activate(engine, session)
    session.state = starting_state

    transition = engine.cancel(session, now=NOW + timedelta(seconds=1))

    assert transition.kind is TransitionKind.CANCELLED
    assert session.state is SessionState.CANCELLED
    assert session.cancelled_at == NOW + timedelta(seconds=1)


def test_cancelled_state_is_terminal(engine: SafetyEngine, session: SafetySession) -> None:
    activate(engine, session)
    engine.cancel(session, now=NOW + timedelta(seconds=1))

    assert engine.advance(session, now=NOW + timedelta(days=1)).kind is TransitionKind.NO_CHANGE
    with pytest.raises(InvalidTransition):
        engine.request_sos(session, now=NOW + timedelta(days=1))
    with pytest.raises(InvalidTransition):
        engine.confirm_safe(session, now=NOW + timedelta(days=1))
    with pytest.raises(InvalidTransition):
        engine.cancel(session, now=NOW + timedelta(days=1))


def test_technical_issue_defers_due_check_in_without_alert(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    activate(engine, session)
    reported = engine.report_technical_issue(
        session,
        TechnicalIssue.NETWORK_DISCONNECTED,
        now=NOW + timedelta(seconds=55),
    )

    deferred = engine.advance(session, now=NOW + timedelta(seconds=60))

    assert reported.kind is TransitionKind.TECHNICAL_ISSUE_REPORTED
    assert deferred.kind is TransitionKind.CHECK_IN_DEFERRED
    assert deferred.state_changed is False
    assert session.state is SessionState.ACTIVE
    assert session.next_check_in_at == NOW + timedelta(seconds=63)
    assert session.alert_reason is None


def test_technical_issue_at_response_deadline_does_not_count_as_miss(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    start_check_in(engine, session)
    engine.report_technical_issue(
        session,
        TechnicalIssue.MICROPHONE_DENIED,
        now=NOW + timedelta(seconds=69),
    )

    transition = engine.advance(session, now=NOW + timedelta(seconds=70))

    assert transition.kind is TransitionKind.CHECK_IN_DEFERRED
    assert session.state is SessionState.CHECK_IN
    assert session.missed_attempts == 0
    assert session.response_deadline == NOW + timedelta(seconds=73)


def test_resolving_issue_resumes_normal_timeout_processing(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    start_check_in(engine, session)
    engine.report_technical_issue(
        session,
        TechnicalIssue.VOICE_UNAVAILABLE,
        now=NOW + timedelta(seconds=69),
    )
    engine.advance(session, now=NOW + timedelta(seconds=70))

    resolved = engine.resolve_technical_issue(
        session,
        TechnicalIssue.VOICE_UNAVAILABLE,
        now=NOW + timedelta(seconds=71),
    )
    missed = engine.advance(session, now=NOW + timedelta(seconds=73))

    assert resolved.kind is TransitionKind.TECHNICAL_ISSUE_RESOLVED
    assert missed.kind is TransitionKind.CHECK_IN_MISSED
    assert session.state is SessionState.UNCONFIRMED


def test_duplicate_technical_reports_are_noops(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    activate(engine, session)
    engine.report_technical_issue(
        session,
        TechnicalIssue.AUDIO_INTERRUPTED,
        now=NOW,
    )

    duplicate = engine.report_technical_issue(
        session,
        TechnicalIssue.AUDIO_INTERRUPTED,
        now=NOW,
    )

    assert duplicate.kind is TransitionKind.NO_CHANGE
    assert session.technical_issues == {TechnicalIssue.AUDIO_INTERRUPTED}


@pytest.mark.parametrize("state", [SessionState.IDLE, SessionState.ALERT, SessionState.CANCELLED])
def test_confirmation_rejects_invalid_states(
    engine: SafetyEngine,
    session: SafetySession,
    state: SessionState,
) -> None:
    session.state = state

    with pytest.raises(InvalidTransition):
        engine.confirm_safe(session, now=NOW)


def test_late_scheduler_still_gives_full_response_window(
    engine: SafetyEngine,
    session: SafetySession,
) -> None:
    activate(engine, session)
    late = NOW + timedelta(minutes=10)

    engine.advance(session, now=late)

    assert session.state is SessionState.CHECK_IN
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
    arguments = {field: value}
    with pytest.raises(ValueError, match=field):
        SafetyPolicy(**arguments)


def test_policy_requires_a_retry_after_the_first_attempt() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        SafetyPolicy(max_attempts=1)
