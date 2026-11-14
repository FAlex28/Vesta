from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.errors import AuthenticationError, ConflictError, NotFoundError
from app.realtime.broker import EventBroker
from app.safety import (
    InvalidTransition,
    SafetyEngine,
    SafetySession,
    SafetyTransition,
    SessionState,
    TechnicalIssue,
    TransitionKind,
)
from app.security import ControlTokenError, ControlTokenService


@dataclass(frozen=True, slots=True)
class CommandResult:
    session: SafetySession
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class ActivationResult(CommandResult):
    control_token: str = ""
    control_token_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _ProcessedCommand:
    fingerprint: str
    session_id: str


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    state: SessionState
    sessions: list[SafetySession]
    latest_sequence: int


_EVENT_TYPES: dict[TransitionKind, str] = {
    TransitionKind.ACTIVATED: "session.activated",
    TransitionKind.CHECK_IN_STARTED: "safety.check_in_requested",
    TransitionKind.CHECK_IN_MISSED: "safety.check_in_unconfirmed",
    TransitionKind.CHECK_IN_CONFIRMED: "safety.check_in_confirmed",
    TransitionKind.CHECK_IN_DEFERRED: "safety.check_in_deferred",
    TransitionKind.ALERTED: "safety.alerted",
    TransitionKind.CANCELLED: "session.cancelled",
    TransitionKind.TECHNICAL_ISSUE_REPORTED: "technical.issue_reported",
    TransitionKind.TECHNICAL_ISSUE_RESOLVED: "technical.issue_resolved",
}


class SessionManager:
    """Serialize session commands, idempotency and real-time event publication."""

    def __init__(
        self,
        *,
        engine: SafetyEngine,
        broker: EventBroker,
        token_service: ControlTokenService,
        control_token_ttl_seconds: int,
        scheduler_tick_seconds: float,
        client_heartbeat_timeout_seconds: float,
    ) -> None:
        self._engine = engine
        self._broker = broker
        self._token_service = token_service
        self._control_token_ttl_seconds = control_token_ttl_seconds
        self._scheduler_tick_seconds = scheduler_tick_seconds
        self._client_heartbeat_timeout = timedelta(seconds=client_heartbeat_timeout_seconds)
        self._sessions: dict[str, SafetySession] = {}
        self._active_session_by_device: dict[str, str] = {}
        self._processed_commands: OrderedDict[str, _ProcessedCommand] = OrderedDict()
        self._max_processed_commands = 10_000
        self._lock = asyncio.Lock()

    async def activate(
        self,
        *,
        event_id: str,
        device_id: str,
        now: datetime | None = None,
    ) -> ActivationResult:
        instant = _utc_now(now)
        fingerprint = _fingerprint("activate", device_id)

        async with self._lock:
            duplicate = self._find_duplicate(event_id, fingerprint)
            if duplicate is not None:
                session = self._sessions[duplicate.session_id]
                token, expires_at = self._issue_control_token(session.session_id, instant)
                return ActivationResult(
                    session=copy.deepcopy(session),
                    duplicate=True,
                    control_token=token,
                    control_token_expires_at=expires_at,
                )

            active_id = self._active_session_by_device.get(device_id)
            if active_id is not None:
                active = self._sessions[active_id]
                if active.state is not SessionState.CANCELLED:
                    raise ConflictError(
                        "SESSION_ALREADY_ACTIVE",
                        f"Device {device_id!r} already has an unfinished session",
                    )

            session = SafetySession(session_id=str(uuid4()), device_id=device_id)
            transition = self._engine.activate(session, now=instant)
            self._sessions[session.session_id] = session
            self._active_session_by_device[device_id] = session.session_id
            self._remember(event_id, fingerprint, session.session_id)
            token, expires_at = self._issue_control_token(session.session_id, instant)
            await self._publish_transition(session, transition)
            return ActivationResult(
                session=copy.deepcopy(session),
                control_token=token,
                control_token_expires_at=expires_at,
            )

    async def confirm_safe(
        self,
        session_id: str,
        *,
        token: str,
        event_id: str,
        source: str,
        now: datetime | None = None,
    ) -> CommandResult:
        instant = _utc_now(now)
        fingerprint = _fingerprint("confirm_safe", session_id, source)
        async with self._lock:
            session = self._get_authorized(session_id, token, instant)
            duplicate = self._find_duplicate(event_id, fingerprint)
            if duplicate is not None:
                return CommandResult(copy.deepcopy(session), duplicate=True)

            timed_transition = self._engine.advance(session, now=instant)
            await self._publish_transition(session, timed_transition)
            try:
                transition = self._engine.confirm_safe(session, now=instant)
            except InvalidTransition as exc:
                raise _transition_conflict(exc) from exc
            self._remember(event_id, fingerprint, session_id)
            await self._publish_transition(session, transition, extra={"source": source})
            return CommandResult(copy.deepcopy(session))

    async def request_sos(
        self,
        session_id: str,
        *,
        token: str,
        event_id: str,
        source: str,
        reason: str | None,
        now: datetime | None = None,
    ) -> CommandResult:
        instant = _utc_now(now)
        fingerprint = _fingerprint("sos", session_id, source, reason or "")
        async with self._lock:
            session = self._get_authorized(session_id, token, instant)
            duplicate = self._find_duplicate(event_id, fingerprint)
            if duplicate is not None:
                return CommandResult(copy.deepcopy(session), duplicate=True)
            try:
                transition = self._engine.request_sos(session, now=instant)
            except InvalidTransition as exc:
                raise _transition_conflict(exc) from exc
            self._remember(event_id, fingerprint, session_id)
            await self._publish_transition(
                session,
                transition,
                extra={"source": source},
            )
            return CommandResult(copy.deepcopy(session))

    async def cancel(
        self,
        session_id: str,
        *,
        token: str,
        event_id: str,
        reason: str | None,
        now: datetime | None = None,
    ) -> CommandResult:
        instant = _utc_now(now)
        fingerprint = _fingerprint("cancel", session_id, reason or "")
        async with self._lock:
            session = self._get_authorized(session_id, token, instant)
            duplicate = self._find_duplicate(event_id, fingerprint)
            if duplicate is not None:
                return CommandResult(copy.deepcopy(session), duplicate=True)
            try:
                transition = self._engine.cancel(session, now=instant)
            except InvalidTransition as exc:
                raise _transition_conflict(exc) from exc
            self._remember(event_id, fingerprint, session_id)
            self._active_session_by_device.pop(session.device_id, None)
            await self._publish_transition(session, transition)
            return CommandResult(copy.deepcopy(session))

    async def heartbeat(
        self,
        session_id: str,
        *,
        token: str,
        now: datetime | None = None,
    ) -> CommandResult:
        """Renew the authenticated Android connectivity lease.

        Once a client starts heartbeats, an expired lease is recorded as a
        technical network issue before the safety engine evaluates timeouts.
        """

        instant = _utc_now(now)
        async with self._lock:
            session = self._get_authorized(session_id, token, instant)
            if session.state is SessionState.CANCELLED:
                raise ConflictError("SESSION_CANCELLED", "A cancelled session has no heartbeat")
            session.client_last_seen_at = instant
            session.connection_lease_expires_at = instant + self._client_heartbeat_timeout
            if TechnicalIssue.NETWORK_DISCONNECTED in session.technical_issues:
                transition = self._engine.resolve_technical_issue(
                    session,
                    TechnicalIssue.NETWORK_DISCONNECTED,
                    now=instant,
                )
                await self._publish_transition(
                    session, transition, extra={"source": "android_heartbeat"}
                )
            return CommandResult(copy.deepcopy(session))

    async def set_technical_issue(
        self,
        session_id: str,
        *,
        issue: TechnicalIssue,
        active: bool,
        event_id: str,
        source: str,
        token: str | None = None,
        trusted_internal: bool = False,
        now: datetime | None = None,
    ) -> CommandResult:
        instant = _utc_now(now)
        fingerprint = _fingerprint("technical_issue", session_id, issue.value, str(active), source)
        async with self._lock:
            session = self._get(session_id)
            if trusted_internal and session.state is SessionState.CANCELLED:
                return CommandResult(copy.deepcopy(session))
            if not trusted_internal:
                if token is None:
                    raise AuthenticationError()
                self._verify_token(token, session_id, instant)
            if not trusted_internal:
                duplicate = self._find_duplicate(event_id, fingerprint)
                if duplicate is not None:
                    return CommandResult(copy.deepcopy(session), duplicate=True)
            try:
                if active:
                    transition = self._engine.report_technical_issue(session, issue, now=instant)
                else:
                    transition = self._engine.resolve_technical_issue(session, issue, now=instant)
            except InvalidTransition as exc:
                raise _transition_conflict(exc) from exc
            if not trusted_internal:
                self._remember(event_id, fingerprint, session_id)
            await self._publish_transition(session, transition, extra={"source": source})
            return CommandResult(copy.deepcopy(session))

    async def get(self, session_id: str) -> SafetySession:
        async with self._lock:
            return copy.deepcopy(self._get(session_id))

    async def authorize(
        self,
        session_id: str,
        token: str,
        *,
        now: datetime | None = None,
    ) -> SafetySession:
        instant = _utc_now(now)
        async with self._lock:
            session = self._get(session_id)
            self._verify_token(token, session_id, instant)
            return copy.deepcopy(session)

    async def list_sessions(self) -> list[SafetySession]:
        async with self._lock:
            return [copy.deepcopy(session) for session in self._sessions.values()]

    async def advance_all(self, *, now: datetime | None = None) -> list[SafetyTransition]:
        instant = _utc_now(now)
        transitions: list[SafetyTransition] = []
        async with self._lock:
            for session in self._sessions.values():
                connection_transition = self._expire_connectivity_lease(session, instant)
                if connection_transition is not None:
                    transitions.append(connection_transition)
                    await self._publish_transition(
                        session,
                        connection_transition,
                        extra={"source": "heartbeat_timeout"},
                    )
                transition = self._engine.advance(session, now=instant)
                if transition.kind is not TransitionKind.NO_CHANGE:
                    transitions.append(transition)
                    await self._publish_transition(session, transition)
        return transitions

    async def run_scheduler(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.advance_all(now=datetime.now(UTC))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._scheduler_tick_seconds)
            except TimeoutError:
                pass

    async def system_state(self) -> SessionState:
        return (await self.snapshot()).state

    async def snapshot(self) -> SystemSnapshot:
        async with self._lock:
            sessions = [copy.deepcopy(session) for session in self._sessions.values()]
            state = self._state_from_sessions(sessions)
            latest_sequence = await self._broker.current_sequence()
            return SystemSnapshot(
                state=state,
                sessions=sessions,
                latest_sequence=latest_sequence,
            )

    def _expire_connectivity_lease(
        self, session: SafetySession, now: datetime
    ) -> SafetyTransition | None:
        lease = session.connection_lease_expires_at
        if (
            lease is None
            or now < lease
            or session.state in {SessionState.ALERT, SessionState.CANCELLED}
            or TechnicalIssue.NETWORK_DISCONNECTED in session.technical_issues
        ):
            return None
        return self._engine.report_technical_issue(
            session,
            TechnicalIssue.NETWORK_DISCONNECTED,
            now=now,
        )

    @staticmethod
    def _state_from_sessions(sessions: list[SafetySession]) -> SessionState:
        live_states = {
            session.state for session in sessions if session.state is not SessionState.CANCELLED
        }
        for state in (
            SessionState.ALERT,
            SessionState.UNCONFIRMED,
            SessionState.CHECK_IN,
            SessionState.ACTIVE,
        ):
            if state in live_states:
                return state
        return SessionState.IDLE

    def _get_authorized(self, session_id: str, token: str, now: datetime) -> SafetySession:
        session = self._get(session_id)
        self._verify_token(token, session_id, now)
        return session

    def _get(self, session_id: str) -> SafetySession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise NotFoundError("Session not found") from exc

    def _verify_token(self, token: str, session_id: str, now: datetime) -> None:
        try:
            self._token_service.verify(token, session_id, now=now)
        except (ControlTokenError, ValueError) as exc:
            raise AuthenticationError() from exc

    def _issue_control_token(self, session_id: str, now: datetime) -> tuple[str, datetime]:
        token = self._token_service.issue(
            session_id,
            now=now,
            ttl=self._control_token_ttl_seconds,
        )
        claims = self._token_service.verify(token, session_id, now=now)
        return token, claims.expires_at

    def _find_duplicate(self, event_id: str, fingerprint: str) -> _ProcessedCommand | None:
        record = self._processed_commands.get(event_id)
        if record is None:
            return None
        if not hmac.compare_digest(record.fingerprint, fingerprint):
            raise ConflictError(
                "EVENT_ID_REUSED",
                "The event_id was already used for a different command",
            )
        self._processed_commands.move_to_end(event_id)
        return record

    def _remember(self, event_id: str, fingerprint: str, session_id: str) -> None:
        self._processed_commands[event_id] = _ProcessedCommand(fingerprint, session_id)
        self._processed_commands.move_to_end(event_id)
        if len(self._processed_commands) > self._max_processed_commands:
            self._processed_commands.popitem(last=False)

    async def _publish_transition(
        self,
        session: SafetySession,
        transition: SafetyTransition,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if transition.kind is TransitionKind.NO_CHANGE:
            return
        event_type = _EVENT_TYPES[transition.kind]
        data: dict[str, Any] = {
            "from_state": transition.from_state.value,
            "state": transition.to_state.value,
            **transition.details,
        }
        if extra:
            data.update({key: value for key, value in extra.items() if value is not None})
        await self._broker.publish(
            event_type,
            session_id=session.session_id,
            device_id=session.device_id,
            data=data,
        )


def _utc_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


def _fingerprint(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _transition_conflict(exc: InvalidTransition) -> ConflictError:
    return ConflictError(
        "INVALID_SESSION_TRANSITION",
        str(exc),
    )
