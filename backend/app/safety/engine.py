"""Pure, deterministic transition rules for Vesta safety sessions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from .models import (
    AlertReason,
    CancellationReason,
    CheckInStatus,
    ConnectivityStatus,
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


class InvalidCheckIn(ValueError):
    """Raised for a missing, stale, mismatched, or expired check-in ID."""

    def __init__(self, message: str, checkin_id: str | None = None) -> None:
        super().__init__(message)
        self.checkin_id = checkin_id


class SafetyEngine:
    """Apply Vesta's safety rules without network, persistence, or AI input."""

    def __init__(
        self,
        policy: SafetyPolicy | None = None,
        *,
        checkin_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.policy = policy or SafetyPolicy()
        self._checkin_id_factory = checkin_id_factory or (lambda: str(uuid4()))
        self._issued_checkin_ids: set[str] = set()

    def activate(self, session: SafetySession, *, now: datetime) -> SafetyTransition:
        """Move a newly-created session from ``IDLE`` to ``ACTIVE``."""

        self._require_aware(now)
        self._require_state(session, "activate", SessionState.IDLE)
        previous = session.state
        session.state = SessionState.ACTIVE
        session.created_at = session.created_at or now
        session.activated_at = now
        session.next_check_in_at = now + self.policy.check_in_interval
        session.response_deadline = None
        session.retry_at = None
        session.quick_cancel_until = now + self.policy.quick_cancel_window
        session.current_attempt = 0
        session.missed_attempts = 0
        session.undelivered_attempts = 0
        self._clear_current_checkin(session)
        session.alert_reason = None
        session.cancelled_at = None
        session.cancellation_reason = None
        self._touch(session, now)
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
            if self._blocks_request_creation(session):
                return self._defer(session, now, deadline_field="next_check_in_at")
            return self._start_check_in(session, now)

        if session.state is SessionState.CHECK_IN:
            if session.response_deadline is None or now < session.response_deadline:
                return self._no_change(session, now)
            if session.current_checkin_status in {
                CheckInStatus.REQUESTED,
                CheckInStatus.UNDELIVERED,
            }:
                return self._expire_undelivered_checkin(session, now)
            if session.current_checkin_status is CheckInStatus.DELIVERED:
                if self._blocks_missed_check_in(session):
                    return self._defer(session, now, deadline_field="response_deadline")
                return self._miss_check_in(session, now)
            raise InvalidCheckIn(
                "Active CHECK_IN session has no request awaiting a result",
                session.current_checkin_id,
            )

        if session.state is SessionState.UNCONFIRMED:
            if session.retry_at is None or now < session.retry_at:
                return self._no_change(session, now)
            if self._blocks_request_creation(session):
                return self._defer(session, now, deadline_field="retry_at")
            return self._start_check_in(session, now)

        raise AssertionError(f"Unhandled safety state: {session.state}")

    def mark_checkin_delivery(
        self,
        session: SafetySession,
        checkin_id: str,
        *,
        delivered: bool,
        now: datetime,
        channel: str = "voice",
    ) -> SafetyTransition:
        """Record whether the active check-in reached its voice/audio channel.

        An undelivered voice prompt remains an active check-in until its
        deadline, so the same ID can still be confirmed through the web UI.
        """

        self._require_aware(now)
        self._require_state(session, "mark check-in delivery", SessionState.CHECK_IN)
        self._require_matching_checkin(session, checkin_id, action="mark delivery")
        self._require_before_deadline(session, checkin_id, now)
        desired = CheckInStatus.DELIVERED if delivered else CheckInStatus.UNDELIVERED

        if session.current_checkin_status is desired:
            return self._no_change(session, now, checkin_id=checkin_id)
        if session.current_checkin_status is not CheckInStatus.REQUESTED:
            raise InvalidCheckIn(
                f"Check-in {checkin_id!r} already has a delivery result",
                checkin_id,
            )

        previous = session.state
        session.current_checkin_status = desired
        if delivered:
            session.current_checkin_delivered_at = now
            session.current_checkin_channel = channel
            kind = TransitionKind.CHECK_IN_DELIVERED
        else:
            session.undelivered_attempts += 1
            kind = TransitionKind.CHECK_IN_UNDELIVERED
        self._touch(session, now)
        return self._transition(
            kind,
            previous,
            session,
            now,
            checkin_id=checkin_id,
            status=desired.value,
            delivered=delivered,
        )

    def mark_checkin_delivered(
        self,
        session: SafetySession,
        checkin_id: str,
        *,
        now: datetime,
    ) -> SafetyTransition:
        """Compatibility convenience for a positive delivery result."""

        return self.mark_checkin_delivery(
            session,
            checkin_id,
            delivered=True,
            now=now,
        )

    def confirm_safe(
        self,
        session: SafetySession,
        checkin_id: str,
        *,
        now: datetime,
    ) -> SafetyTransition:
        """Accept an on-time confirmation for the active, matching check-in."""

        self._require_aware(now)
        self._require_state(session, "confirm safety", SessionState.CHECK_IN)
        self._require_matching_checkin(session, checkin_id, action="confirm")
        self._require_before_deadline(session, checkin_id, now)
        if session.current_checkin_status not in {
            CheckInStatus.REQUESTED,
            CheckInStatus.DELIVERED,
            CheckInStatus.UNDELIVERED,
        }:
            raise InvalidCheckIn(f"Check-in {checkin_id!r} is no longer active", checkin_id)

        previous = session.state
        self._archive_current_checkin(session, CheckInStatus.CONFIRMED)
        session.last_confirmation_at = now
        session.state = SessionState.ACTIVE
        session.next_check_in_at = now + self.policy.check_in_interval
        session.response_deadline = None
        session.retry_at = None
        session.current_attempt = 0
        session.missed_attempts = 0
        session.undelivered_attempts = 0
        self._touch(session, now)
        return self._transition(
            TransitionKind.CHECK_IN_CONFIRMED,
            previous,
            session,
            now,
            checkin_id=checkin_id,
            status=CheckInStatus.CONFIRMED.value,
            next_check_in_at=session.next_check_in_at.isoformat(),
        )

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
        return self._alert(session, now, AlertReason.SOS_EXPLICIT)

    def cancel(
        self,
        session: SafetySession,
        *,
        now: datetime,
        reason: str | CancellationReason | None = None,
    ) -> SafetyTransition:
        """Cancel a non-alerting active session with an auditable reason."""

        self._require_aware(now)
        self._require_state(
            session,
            "cancel",
            SessionState.ACTIVE,
            SessionState.CHECK_IN,
            SessionState.UNCONFIRMED,
        )
        quick_cancel = session.is_within_quick_cancel_window(now)
        if reason is None:
            reason = (
                CancellationReason.ACCIDENTAL_ACTIVATION
                if quick_cancel
                else CancellationReason.USER_CANCELLED
            )
        normalized_reason = self._normalize_cancellation_reason(reason)
        previous = session.state
        checkin_id = session.current_checkin_id
        if checkin_id is not None and session.current_checkin_status is not None:
            self._archive_current_checkin(session, session.current_checkin_status)
        session.state = SessionState.CANCELLED
        session.cancelled_at = now
        session.cancellation_reason = normalized_reason
        self._clear_deadlines(session)
        self._touch(session, now)
        return self._transition(
            TransitionKind.CANCELLED,
            previous,
            session,
            now,
            checkin_id=checkin_id,
            reason=normalized_reason,
            quick_cancel=quick_cancel,
        )

    def resolve_alert(self, session: SafetySession, *, now: datetime) -> SafetyTransition:
        """Resolve an alert explicitly; ordinary cancellation cannot close it."""

        self._require_aware(now)
        self._require_state(session, "resolve alert", SessionState.ALERT)
        previous = session.state
        checkin_id = session.current_checkin_id
        if checkin_id is not None and session.current_checkin_status is not None:
            self._archive_current_checkin(session, session.current_checkin_status)
        session.state = SessionState.CANCELLED
        session.cancelled_at = now
        session.cancellation_reason = CancellationReason.ALERT_RESOLVED.value
        self._clear_deadlines(session)
        self._touch(session, now)
        return self._transition(
            TransitionKind.ALERT_RESOLVED,
            previous,
            session,
            now,
            checkin_id=checkin_id,
            reason=CancellationReason.ALERT_RESOLVED.value,
            alert_reason=session.alert_reason.value if session.alert_reason else None,
        )

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
        self._touch(session, now)
        return self._transition(
            TransitionKind.TECHNICAL_ISSUE_REPORTED,
            previous,
            session,
            now,
            issue=issue.value,
            blocking=issue.blocks_check_in,
            health_status=session.health_status.value,
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
        self._touch(session, now)
        return self._transition(
            TransitionKind.TECHNICAL_ISSUE_RESOLVED,
            previous,
            session,
            now,
            issue=issue.value,
            blocking=issue.blocks_check_in,
            health_status=session.health_status.value,
        )

    def _start_check_in(self, session: SafetySession, now: datetime) -> SafetyTransition:
        previous = session.state
        checkin_id = self._new_checkin_id(session)
        session.state = SessionState.CHECK_IN
        session.current_attempt = session.missed_attempts + 1
        session.next_check_in_at = None
        session.retry_at = None
        session.response_deadline = now + self.policy.response_timeout
        session.current_checkin_id = checkin_id
        session.current_checkin_status = CheckInStatus.REQUESTED
        session.current_checkin_requested_at = now
        session.current_checkin_delivered_at = None
        self._touch(session, now)
        return self._transition(
            TransitionKind.CHECK_IN_STARTED,
            previous,
            session,
            now,
            checkin_id=checkin_id,
            attempt=session.current_attempt,
            status=CheckInStatus.REQUESTED.value,
            response_deadline=session.response_deadline.isoformat(),
        )

    def _expire_undelivered_checkin(
        self,
        session: SafetySession,
        now: datetime,
    ) -> SafetyTransition:
        previous = session.state
        checkin_id = session.current_checkin_id
        if checkin_id is None:
            raise InvalidCheckIn("Cannot expire a check-in without an ID")
        if session.current_checkin_status is CheckInStatus.REQUESTED:
            session.undelivered_attempts += 1
        self._archive_current_checkin(session, CheckInStatus.UNDELIVERED)
        session.state = SessionState.UNCONFIRMED
        session.response_deadline = None
        session.retry_at = now + self.policy.retry_delay
        self._touch(session, now)
        return self._transition(
            TransitionKind.CHECK_IN_UNDELIVERED,
            previous,
            session,
            now,
            checkin_id=checkin_id,
            status=CheckInStatus.UNDELIVERED.value,
            missed_attempts=session.missed_attempts,
            undelivered_attempts=session.undelivered_attempts,
            retry_at=session.retry_at.isoformat(),
        )

    def _miss_check_in(self, session: SafetySession, now: datetime) -> SafetyTransition:
        previous = session.state
        checkin_id = session.current_checkin_id
        if checkin_id is None:
            raise InvalidCheckIn("Cannot time out a check-in without an ID")
        session.missed_attempts += 1
        self._archive_current_checkin(session, CheckInStatus.TIMED_OUT)

        if session.missed_attempts >= self.policy.max_attempts:
            session.state = SessionState.ALERT
            session.alert_reason = AlertReason.MISSED_CHECK_INS
            self._clear_deadlines(session)
            self._touch(session, now)
            return self._transition(
                TransitionKind.ALERTED,
                previous,
                session,
                now,
                checkin_id=checkin_id,
                status=CheckInStatus.TIMED_OUT.value,
                reason=AlertReason.MISSED_CHECK_INS.value,
                missed_attempts=session.missed_attempts,
            )

        session.state = SessionState.UNCONFIRMED
        session.response_deadline = None
        session.retry_at = now + self.policy.retry_delay
        self._touch(session, now)
        return self._transition(
            TransitionKind.CHECK_IN_MISSED,
            previous,
            session,
            now,
            checkin_id=checkin_id,
            status=CheckInStatus.TIMED_OUT.value,
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
        checkin_id = session.current_checkin_id
        session.state = SessionState.ALERT
        session.alert_reason = reason
        self._clear_deadlines(session)
        self._touch(session, now)
        return self._transition(
            TransitionKind.ALERTED,
            previous,
            session,
            now,
            checkin_id=checkin_id,
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
        deferred_until = now + self.policy.technical_issue_retry_delay
        setattr(session, deadline_field, deferred_until)
        self._touch(session, now)
        return self._transition(
            TransitionKind.CHECK_IN_DEFERRED,
            session.state,
            session,
            now,
            technical_issues=sorted(
                issue.value for issue in session.technical_issues if issue.blocks_check_in
            ),
            health_status=session.health_status.value,
            deferred_until=deferred_until.isoformat(),
        )

    @staticmethod
    def _blocks_request_creation(session: SafetySession) -> bool:
        """Return whether even creating a voice request must be postponed.

        A disconnected audio output is deliberately excluded: the request and
        its ID must still exist so the web confirmation fallback can work.
        """

        blocking_issues = {
            issue
            for issue in session.technical_issues
            if issue is TechnicalIssue.NETWORK_DISCONNECTED
        }
        return (
            bool(blocking_issues) or session.connectivity_status is ConnectivityStatus.DISCONNECTED
        )

    def _new_checkin_id(self, session: SafetySession) -> str:
        for _ in range(100):
            candidate = str(self._checkin_id_factory()).strip()
            if not candidate:
                continue
            if candidate in self._issued_checkin_ids:
                continue
            if candidate in {session.current_checkin_id, session.last_checkin_id}:
                continue
            self._issued_checkin_ids.add(candidate)
            return candidate
        raise RuntimeError("checkin_id_factory did not produce a unique non-empty ID")

    @staticmethod
    def _archive_current_checkin(
        session: SafetySession,
        final_status: CheckInStatus,
    ) -> None:
        session.last_checkin_id = session.current_checkin_id
        session.last_checkin_status = final_status
        session.last_checkin_requested_at = session.current_checkin_requested_at
        session.last_checkin_delivered_at = session.current_checkin_delivered_at
        SafetyEngine._clear_current_checkin(session)

    @staticmethod
    def _clear_current_checkin(session: SafetySession) -> None:
        session.current_checkin_id = None
        session.current_checkin_status = None
        session.current_checkin_requested_at = None
        session.current_checkin_delivered_at = None
        session.current_checkin_channel = None

    @staticmethod
    def _blocks_missed_check_in(session: SafetySession) -> bool:
        """Whether an unanswered, delivered check-in must be deferred, not counted.

        A prompt delivered by voice depends on the audio path, so any blocking
        technical issue defers it. A prompt shown on the phone screen ("web" or
        "android") was seen through the page itself: only losing the phone's
        connection prevents an answer, so audio problems do not defer it.
        """

        if session.current_checkin_channel in {"web", "android"}:
            return (
                TechnicalIssue.NETWORK_DISCONNECTED in session.technical_issues
                or session.connectivity_status is ConnectivityStatus.DISCONNECTED
            )
        return session.has_blocking_technical_issue

    @staticmethod
    def _clear_deadlines(session: SafetySession) -> None:
        session.next_check_in_at = None
        session.response_deadline = None
        session.retry_at = None

    @staticmethod
    def _touch(session: SafetySession, now: datetime) -> None:
        session.updated_at = now
        session.version += 1

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
    def _require_matching_checkin(
        session: SafetySession,
        checkin_id: str,
        *,
        action: str,
    ) -> None:
        if not checkin_id or session.current_checkin_id != checkin_id:
            raise InvalidCheckIn(
                f"Cannot {action}: check-in ID does not match the active request",
                checkin_id,
            )

    @staticmethod
    def _require_before_deadline(
        session: SafetySession,
        checkin_id: str,
        now: datetime,
    ) -> None:
        if session.response_deadline is None:
            raise InvalidCheckIn(f"Check-in {checkin_id!r} has no active deadline", checkin_id)
        if now >= session.response_deadline:
            raise InvalidCheckIn(f"Check-in {checkin_id!r} has expired", checkin_id)

    @staticmethod
    def _normalize_cancellation_reason(reason: str | CancellationReason) -> str:
        value = reason.value if isinstance(reason, CancellationReason) else str(reason).strip()
        if not value:
            raise ValueError("cancellation reason must not be empty")
        return value

    @staticmethod
    def _transition(
        kind: TransitionKind,
        previous: SessionState,
        session: SafetySession,
        now: datetime,
        *,
        checkin_id: str | None = None,
        **details: object,
    ) -> SafetyTransition:
        effective_checkin_id = checkin_id or session.current_checkin_id
        payload = dict(details)
        payload.setdefault("version", session.version)
        if effective_checkin_id is not None:
            payload.setdefault("checkin_id", effective_checkin_id)
        return SafetyTransition(
            kind=kind,
            from_state=previous,
            to_state=session.state,
            occurred_at=now,
            version=session.version,
            checkin_id=effective_checkin_id,
            details=payload,
        )

    @staticmethod
    def _no_change(
        session: SafetySession,
        now: datetime,
        *,
        checkin_id: str | None = None,
        **details: object,
    ) -> SafetyTransition:
        return SafetyEngine._transition(
            TransitionKind.NO_CHANGE,
            session.state,
            session,
            now,
            checkin_id=checkin_id,
            **details,
        )
