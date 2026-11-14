"""Pure, deterministic transition rules for Vesta safety sessions."""

from __future__ import annotations

from datetime import datetime

from .models import (
    AlertReason,
    SafetyPolicy,
    SafetySession,
    SafetyTransition,
    SessionState,
    TechnicalIssue,
    TransitionKind,
)


class InvalidTransition(ValueError):
    """Raised when a caller asks for a transition forbidden by the policy."""

    def __init__(self, action: str, state: SessionState) -> None:
        super().__init__(f"Cannot {action} while session is {state.value}")
        self.action = action
        self.state = state


class SafetyEngine:
    """Apply Vesta's safety rules without network, persistence or AI input."""

    def __init__(self, policy: SafetyPolicy | None = None) -> None:
        self.policy = policy or SafetyPolicy()

    def activate(self, session: SafetySession, *, now: datetime) -> SafetyTransition:
        """Move a newly-created session from ``IDLE`` to ``ACTIVE``."""

        self._require_aware(now)
        self._require_state(session, "activate", SessionState.IDLE)
        previous = session.state
        session.state = SessionState.ACTIVE
        session.created_at = session.created_at or now
        session.activated_at = now
        session.updated_at = now
        session.next_check_in_at = now + self.policy.check_in_interval
        session.response_deadline = None
        session.retry_at = None
        session.current_attempt = 0
        session.missed_attempts = 0
        session.alert_reason = None
        session.cancelled_at = None
        return self._transition(TransitionKind.ACTIVATED, previous, session, now)

    def advance(self, session: SafetySession, *, now: datetime) -> SafetyTransition:
        """Perform at most one transition whose deadline has elapsed.

        A delayed scheduler never skips the user's response window: starting a
        check-in always gives a fresh ``response_timeout`` from ``now``.
        """

        self._require_aware(now)
        if session.state in {SessionState.IDLE, SessionState.ALERT, SessionState.CANCELLED}:
            return self._no_change(session, now)

        if session.state is SessionState.ACTIVE:
            if session.next_check_in_at is None or now < session.next_check_in_at:
                return self._no_change(session, now)
            if session.has_blocking_technical_issue:
                return self._defer(session, now, deadline_field="next_check_in_at")
            return self._start_check_in(session, now)

        if session.state is SessionState.CHECK_IN:
            if session.response_deadline is None or now < session.response_deadline:
                return self._no_change(session, now)
            if session.has_blocking_technical_issue:
                return self._defer(session, now, deadline_field="response_deadline")
            return self._miss_check_in(session, now)

        if session.state is SessionState.UNCONFIRMED:
            if session.retry_at is None or now < session.retry_at:
                return self._no_change(session, now)
            if session.has_blocking_technical_issue:
                return self._defer(session, now, deadline_field="retry_at")
            return self._start_check_in(session, now)

        raise AssertionError(f"Unhandled safety state: {session.state}")

    def confirm_safe(self, session: SafetySession, *, now: datetime) -> SafetyTransition:
        """Accept an explicit valid confirmation and reset escalation counters."""

        self._require_aware(now)
        self._require_state(
            session,
            "confirm safety",
            SessionState.CHECK_IN,
            SessionState.UNCONFIRMED,
        )
        previous = session.state
        session.state = SessionState.ACTIVE
        session.updated_at = now
        session.next_check_in_at = now + self.policy.check_in_interval
        session.response_deadline = None
        session.retry_at = None
        session.current_attempt = 0
        session.missed_attempts = 0
        return self._transition(TransitionKind.CHECK_IN_CONFIRMED, previous, session, now)

    def request_sos(self, session: SafetySession, *, now: datetime) -> SafetyTransition:
        """Escalate immediately following an explicit user SOS action."""

        self._require_aware(now)
        self._require_state(
            session,
            "request SOS",
            SessionState.ACTIVE,
            SessionState.CHECK_IN,
            SessionState.UNCONFIRMED,
        )
        return self._alert(session, now, AlertReason.EXPLICIT_SOS)

    def cancel(self, session: SafetySession, *, now: datetime) -> SafetyTransition:
        """Cancel an existing session after authorization by the caller layer."""

        self._require_aware(now)
        self._require_state(
            session,
            "cancel",
            SessionState.ACTIVE,
            SessionState.CHECK_IN,
            SessionState.UNCONFIRMED,
            SessionState.ALERT,
        )
        previous = session.state
        session.state = SessionState.CANCELLED
        session.updated_at = now
        session.cancelled_at = now
        self._clear_deadlines(session)
        return self._transition(TransitionKind.CANCELLED, previous, session, now)

    def report_technical_issue(
        self,
        session: SafetySession,
        issue: TechnicalIssue,
        *,
        now: datetime,
    ) -> SafetyTransition:
        """Record a technical issue without changing the safety state."""

        self._require_aware(now)
        self._require_not_cancelled(session, "report technical issue")
        previous = session.state
        if issue in session.technical_issues:
            return self._no_change(session, now, issue=issue.value)
        session.technical_issues.add(issue)
        session.updated_at = now
        return self._transition(
            TransitionKind.TECHNICAL_ISSUE_REPORTED,
            previous,
            session,
            now,
            issue=issue.value,
        )

    def resolve_technical_issue(
        self,
        session: SafetySession,
        issue: TechnicalIssue,
        *,
        now: datetime,
    ) -> SafetyTransition:
        """Clear a technical issue without implying anything about user safety."""

        self._require_aware(now)
        self._require_not_cancelled(session, "resolve technical issue")
        previous = session.state
        if issue not in session.technical_issues:
            return self._no_change(session, now, issue=issue.value)
        session.technical_issues.remove(issue)
        session.updated_at = now
        return self._transition(
            TransitionKind.TECHNICAL_ISSUE_RESOLVED,
            previous,
            session,
            now,
            issue=issue.value,
        )

    def _start_check_in(self, session: SafetySession, now: datetime) -> SafetyTransition:
        previous = session.state
        session.state = SessionState.CHECK_IN
        session.current_attempt = session.missed_attempts + 1
        session.updated_at = now
        session.next_check_in_at = None
        session.retry_at = None
        session.response_deadline = now + self.policy.response_timeout
        return self._transition(
            TransitionKind.CHECK_IN_STARTED,
            previous,
            session,
            now,
            attempt=session.current_attempt,
            response_deadline=session.response_deadline.isoformat(),
        )

    def _miss_check_in(self, session: SafetySession, now: datetime) -> SafetyTransition:
        session.missed_attempts += 1
        if session.missed_attempts >= self.policy.max_attempts:
            return self._alert(session, now, AlertReason.MISSED_CHECK_INS)

        previous = session.state
        session.state = SessionState.UNCONFIRMED
        session.updated_at = now
        session.response_deadline = None
        session.retry_at = now + self.policy.retry_delay
        return self._transition(
            TransitionKind.CHECK_IN_MISSED,
            previous,
            session,
            now,
            missed_attempts=session.missed_attempts,
            retry_at=session.retry_at.isoformat(),
        )

    def _alert(
        self,
        session: SafetySession,
        now: datetime,
        reason: AlertReason,
    ) -> SafetyTransition:
        previous = session.state
        session.state = SessionState.ALERT
        session.updated_at = now
        session.alert_reason = reason
        self._clear_deadlines(session)
        return self._transition(
            TransitionKind.ALERTED,
            previous,
            session,
            now,
            reason=reason.value,
            missed_attempts=session.missed_attempts,
        )

    def _defer(
        self,
        session: SafetySession,
        now: datetime,
        *,
        deadline_field: str,
    ) -> SafetyTransition:
        setattr(session, deadline_field, now + self.policy.technical_issue_retry_delay)
        session.updated_at = now
        return self._transition(
            TransitionKind.CHECK_IN_DEFERRED,
            session.state,
            session,
            now,
            technical_issues=sorted(issue.value for issue in session.technical_issues),
            deferred_until=getattr(session, deadline_field).isoformat(),
        )

    @staticmethod
    def _clear_deadlines(session: SafetySession) -> None:
        session.next_check_in_at = None
        session.response_deadline = None
        session.retry_at = None

    @staticmethod
    def _require_aware(now: datetime) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

    @staticmethod
    def _require_state(
        session: SafetySession,
        action: str,
        *allowed: SessionState,
    ) -> None:
        if session.state not in allowed:
            raise InvalidTransition(action, session.state)

    @staticmethod
    def _require_not_cancelled(session: SafetySession, action: str) -> None:
        if session.state is SessionState.CANCELLED:
            raise InvalidTransition(action, session.state)

    @staticmethod
    def _transition(
        kind: TransitionKind,
        previous: SessionState,
        session: SafetySession,
        now: datetime,
        **details: object,
    ) -> SafetyTransition:
        return SafetyTransition(
            kind=kind,
            from_state=previous,
            to_state=session.state,
            occurred_at=now,
            details=dict(details),
        )

    @staticmethod
    def _no_change(
        session: SafetySession,
        now: datetime,
        **details: object,
    ) -> SafetyTransition:
        return SafetyTransition(
            kind=TransitionKind.NO_CHANGE,
            from_state=session.state,
            to_state=session.state,
            occurred_at=now,
            details=dict(details),
        )
