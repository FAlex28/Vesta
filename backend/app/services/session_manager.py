from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.errors import AuthenticationError, ConflictError, NotFoundError
from app.integrations.persistence import NullRecorder, SessionRecorder, TransitionRecord
from app.realtime.broker import EventBroker
from app.safety import (
    AlertReason,
    AudioStatus,
    CancellationReason,
    ConnectivityStatus,
    GpsStatus,
    InvalidCheckIn,
    InvalidTransition,
    PageVisibility,
    PowerStatus,
    SafetyEngine,
    SafetySession,
    SafetyTransition,
    SessionState,
    TechnicalIssue,
    TransitionKind,
)
from app.security import ControlTokenError, ControlTokenService
from app.services.alerts import (
    AlertIncidentType,
    AlertNotFound,
    AlertRecord,
    AlertRegistry,
    AlertRegistryError,
    AlertSessionMismatch,
    AlertStatus,
)
from app.services.location import (
    AuthorizedLocation,
    AuthorizedLocationProvider,
    NoopAuthorizedLocationProvider,
)
from app.services.recovery import (
    NoopSessionRecoveryProvider,
    RecoveredCommand,
    RecoveredSessionBundle,
    SessionRecoveryProvider,
)
from app.voice import VoiceConnectionStatus

LOGGER = logging.getLogger(__name__)


class ActivationOutcome(StrEnum):
    ACTIVATED = "ACTIVATED"
    DUPLICATE = "DUPLICATE"
    ALREADY_ACTIVE = "ALREADY_ACTIVE"


@dataclass(frozen=True, slots=True)
class CommandResult:
    session: SafetySession
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class ActivationResult(CommandResult):
    outcome: ActivationOutcome = ActivationOutcome.ACTIVATED
    control_token: str = ""
    control_token_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AlertCommandResult(CommandResult):
    alert: AlertRecord | None = None


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
    TransitionKind.CHECK_IN_DELIVERED: "safety.check_in_delivered",
    TransitionKind.CHECK_IN_UNDELIVERED: "safety.check_in_undelivered",
    TransitionKind.CHECK_IN_MISSED: "safety.check_in_unconfirmed",
    TransitionKind.CHECK_IN_CONFIRMED: "safety.check_in_confirmed",
    TransitionKind.CHECK_IN_DEFERRED: "safety.check_in_deferred",
    TransitionKind.ALERTED: "safety.alerted",
    TransitionKind.ALERT_RESOLVED: "safety.alert_resolved",
    TransitionKind.CANCELLED: "session.cancelled",
    TransitionKind.TECHNICAL_ISSUE_REPORTED: "technical.issue_reported",
    TransitionKind.TECHNICAL_ISSUE_RESOLVED: "technical.issue_resolved",
}


class SessionManager:
    """Serialize safety commands, idempotency, alerts, recovery and events."""

    def __init__(
        self,
        *,
        engine: SafetyEngine,
        broker: EventBroker,
        token_service: ControlTokenService,
        control_token_ttl_seconds: int,
        scheduler_tick_seconds: float,
        client_heartbeat_timeout_seconds: float,
        alerts: AlertRegistry | None = None,
        recovery_provider: SessionRecoveryProvider | None = None,
        location_provider: AuthorizedLocationProvider | None = None,
        recorder: SessionRecorder | None = None,
    ) -> None:
        self._engine = engine
        self._broker = broker
        self._token_service = token_service
        self._control_token_ttl_seconds = control_token_ttl_seconds
        self._scheduler_tick_seconds = scheduler_tick_seconds
        self._client_heartbeat_timeout = timedelta(seconds=client_heartbeat_timeout_seconds)
        self._alerts = alerts or AlertRegistry()
        self._recovery_provider = recovery_provider or NoopSessionRecoveryProvider()
        self._location_provider = location_provider or NoopAuthorizedLocationProvider()
        self._sessions: dict[str, SafetySession] = {}
        self._processed_commands: OrderedDict[str, _ProcessedCommand] = OrderedDict()
        self._max_processed_commands = 10_000
        self._lock = asyncio.Lock()
        self._recorder: SessionRecorder = recorder or NullRecorder()

    @property
    def alerts(self) -> AlertRegistry:
        return self._alerts

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
                    outcome=ActivationOutcome.DUPLICATE,
                    control_token=token,
                    control_token_expires_at=expires_at,
                )

            active = self._current_unfinished()
            if active is not None:
                self._remember(event_id, fingerprint, active.session_id)
                await self._broker.publish(
                    "session.already_active",
                    session_id=active.session_id,
                    device_id=active.device_id,
                    data={
                        "outcome": ActivationOutcome.ALREADY_ACTIVE.value,
                        "state": active.state.value,
                        "health_status": active.health_status.value,
                        "version": active.version,
                    },
                )
                token, expires_at = self._issue_control_token(active.session_id, instant)
                return ActivationResult(
                    session=copy.deepcopy(active),
                    outcome=ActivationOutcome.ALREADY_ACTIVE,
                    control_token=token,
                    control_token_expires_at=expires_at,
                )

            session = SafetySession(session_id=str(uuid4()), device_id=device_id)
            transition = self._engine.activate(session, now=instant)
            self._sessions[session.session_id] = session
            self._remember(event_id, fingerprint, session.session_id)
            token, expires_at = self._issue_control_token(session.session_id, instant)
            await self._handle_transition(
                session,
                transition,
                command_event_id=event_id,
            )
            return ActivationResult(
                session=copy.deepcopy(session),
                outcome=ActivationOutcome.ACTIVATED,
                control_token=token,
                control_token_expires_at=expires_at,
            )

    async def confirm_safe(
        self,
        session_id: str,
        *,
        token: str,
        event_id: str,
        checkin_id: str,
        source: str,
        now: datetime | None = None,
    ) -> CommandResult:
        instant = _utc_now(now)
        fingerprint = _fingerprint("confirm_safe", session_id, checkin_id, source)
        async with self._lock:
            session = self._get_authorized(session_id, token, instant)
            duplicate = self._find_duplicate(event_id, fingerprint)
            if duplicate is not None:
                return CommandResult(copy.deepcopy(session), duplicate=True)

            timed_transition = self._engine.advance(session, now=instant)
            await self._handle_transition(session, timed_transition)
            try:
                transition = self._engine.confirm_safe(session, checkin_id, now=instant)
            except InvalidCheckIn as exc:
                raise _checkin_conflict(exc, session, checkin_id) from exc
            except InvalidTransition as exc:
                raise _checkin_state_conflict(exc, session) from exc
            self._remember(event_id, fingerprint, session_id)
            await self._handle_transition(
                session,
                transition,
                extra={"source": source},
                command_event_id=event_id,
            )
            return CommandResult(copy.deepcopy(session))

    async def mark_checkin_delivery(
        self,
        session_id: str,
        checkin_id: str,
        *,
        token: str,
        event_id: str,
        delivered: bool,
        channel: str,
        now: datetime | None = None,
    ) -> CommandResult:
        instant = _utc_now(now)
        fingerprint = _fingerprint(
            "checkin_delivery", session_id, checkin_id, str(delivered), channel
        )
        async with self._lock:
            session = self._get_authorized(session_id, token, instant)
            duplicate = self._find_duplicate(event_id, fingerprint)
            if duplicate is not None:
                return CommandResult(copy.deepcopy(session), duplicate=True)
            timed_transition = self._engine.advance(session, now=instant)
            await self._handle_transition(session, timed_transition)
            try:
                transition = self._engine.mark_checkin_delivery(
                    session,
                    checkin_id,
                    delivered=delivered,
                    now=instant,
                )
            except InvalidCheckIn as exc:
                raise _checkin_conflict(exc, session, checkin_id) from exc
            except InvalidTransition as exc:
                raise _checkin_state_conflict(exc, session) from exc
            await self._handle_transition(
                session,
                transition,
                extra={"channel": channel},
                command_event_id=event_id,
            )
            if channel == "voice":
                session.audio_status = (
                    AudioStatus.CONNECTED if delivered else AudioStatus.DISCONNECTED
                )
                issue_transition = self._set_issue_locked(
                    session,
                    TechnicalIssue.AUDIO_DISCONNECTED,
                    active=not delivered,
                    now=instant,
                )
                await self._handle_transition(
                    session,
                    issue_transition,
                    extra={"source": "checkin_delivery"},
                    command_event_id=event_id,
                )
            self._remember(event_id, fingerprint, session_id)
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
    ) -> AlertCommandResult:
        instant = _utc_now(now)
        fingerprint = _fingerprint("sos", session_id, source, reason or "")
        async with self._lock:
            session = self._get_authorized(session_id, token, instant)
            duplicate = self._find_duplicate(event_id, fingerprint)
            if duplicate is not None:
                return AlertCommandResult(
                    copy.deepcopy(session),
                    duplicate=True,
                    alert=self._alerts.get_for_session(session_id),
                )

            if session.state is SessionState.ALERT:
                if session.alert_reason is not AlertReason.SOS_EXPLICIT:
                    session.alert_reason = AlertReason.SOS_EXPLICIT
                    session.updated_at = instant
                    session.version += 1
                transition = SafetyTransition(
                    kind=TransitionKind.ALERTED,
                    from_state=SessionState.ALERT,
                    to_state=SessionState.ALERT,
                    occurred_at=instant,
                    version=session.version,
                    checkin_id=session.current_checkin_id,
                    details={
                        "reason": AlertReason.SOS_EXPLICIT.value,
                        "already_alerting": True,
                        "version": session.version,
                    },
                )
                alert = await self._handle_transition(
                    session,
                    transition,
                    extra={"source": source},
                    command_event_id=event_id,
                    reason=reason,
                )
            else:
                try:
                    transition = self._engine.request_sos(session, now=instant)
                except InvalidTransition as exc:
                    raise _transition_conflict(exc) from exc
                alert = await self._handle_transition(
                    session,
                    transition,
                    extra={"source": source},
                    command_event_id=event_id,
                    reason=reason,
                )
            self._remember(event_id, fingerprint, session_id)
            return AlertCommandResult(copy.deepcopy(session), alert=alert)

    async def cancel(
        self,
        session_id: str,
        *,
        token: str,
        event_id: str,
        reason: str | None,
        now: datetime | None = None,
    ) -> AlertCommandResult:
        instant = _utc_now(now)
        normalized_reason = reason or CancellationReason.USER_CANCELLED.value
        fingerprint = _fingerprint("cancel", session_id, normalized_reason)
        async with self._lock:
            session = self._get_authorized(session_id, token, instant)
            duplicate = self._find_duplicate(event_id, fingerprint)
            if duplicate is not None:
                return AlertCommandResult(
                    copy.deepcopy(session),
                    duplicate=True,
                    alert=self._alerts.get_for_session(session_id),
                )

            accidental = normalized_reason == CancellationReason.ACCIDENTAL_ACTIVATION.value
            if accidental and not session.is_within_quick_cancel_window(instant):
                raise ConflictError(
                    "QUICK_CANCEL_EXPIRED",
                    "The accidental-activation cancellation window has expired",
                )

            if session.state is SessionState.ALERT:
                if not accidental:
                    raise ConflictError(
                        "ALERT_REQUIRES_RESOLUTION",
                        "A transmitted alert must be resolved explicitly",
                    )
                result = await self._resolve_alert_locked(
                    session,
                    alert_id=None,
                    now=instant,
                    action=CancellationReason.ACCIDENTAL_ACTIVATION.value,
                    cancellation_reason=CancellationReason.ACCIDENTAL_ACTIVATION.value,
                    command_event_id=event_id,
                )
                self._remember(event_id, fingerprint, session_id)
                return result

            try:
                transition = self._engine.cancel(
                    session,
                    now=instant,
                    reason=normalized_reason,
                )
            except InvalidTransition as exc:
                raise _transition_conflict(exc) from exc
            self._remember(event_id, fingerprint, session_id)
            await self._handle_transition(
                session,
                transition,
                command_event_id=event_id,
                reason=normalized_reason,
            )
            return AlertCommandResult(
                copy.deepcopy(session),
                alert=self._alerts.get_for_session(session_id),
            )

    async def resolve_alert(
        self,
        session_id: str,
        alert_id: str,
        *,
        token: str,
        event_id: str,
        action: str,
        now: datetime | None = None,
    ) -> AlertCommandResult:
        instant = _utc_now(now)
        fingerprint = _fingerprint("resolve_alert", session_id, alert_id, action)
        async with self._lock:
            session = self._get_authorized(session_id, token, instant)
            duplicate = self._find_duplicate(event_id, fingerprint)
            if duplicate is not None:
                return AlertCommandResult(
                    copy.deepcopy(session),
                    duplicate=True,
                    alert=self._alerts.get_for_session(session_id),
                )
            result = await self._resolve_alert_locked(
                session,
                alert_id=alert_id,
                now=instant,
                action=action,
                command_event_id=event_id,
            )
            self._remember(event_id, fingerprint, session_id)
            return result

    async def heartbeat(
        self,
        session_id: str,
        *,
        token: str,
        page_visibility: PageVisibility | None = None,
        now: datetime | None = None,
    ) -> CommandResult:
        instant = _utc_now(now)
        async with self._lock:
            session = self._get_authorized(session_id, token, instant)
            if session.state is SessionState.CANCELLED:
                raise ConflictError("SESSION_CANCELLED", "A cancelled session has no heartbeat")
            session.client_last_seen_at = instant
            session.connection_lease_expires_at = instant + self._client_heartbeat_timeout
            session.connectivity_status = ConnectivityStatus.CONNECTED
            if page_visibility is not None:
                session.page_visibility = page_visibility
            transitions = [
                self._set_issue_locked(
                    session,
                    TechnicalIssue.NETWORK_DISCONNECTED,
                    active=False,
                    now=instant,
                )
            ]
            if page_visibility is not None:
                transitions.append(
                    self._set_issue_locked(
                        session,
                        TechnicalIssue.PAGE_HIDDEN,
                        active=page_visibility is PageVisibility.HIDDEN,
                        now=instant,
                    )
                )
            effective = [
                transition
                for transition in transitions
                if transition.kind is not TransitionKind.NO_CHANGE
            ]
            if not effective:
                session.updated_at = instant
                session.version += 1
            for transition in effective:
                await self._handle_transition(
                    session,
                    transition,
                    extra={"source": "client_heartbeat"},
                )
            await self._publish_client_status(session, source="client_heartbeat")
            return CommandResult(copy.deepcopy(session))

    async def update_client_status(
        self,
        session_id: str,
        *,
        token: str,
        event_id: str,
        page_visibility: PageVisibility | None = None,
        connectivity_status: ConnectivityStatus | None = None,
        audio_status: AudioStatus | None = None,
        gps_status: GpsStatus | None = None,
        power_status: PowerStatus | None = None,
        source: str = "client",
        now: datetime | None = None,
    ) -> CommandResult:
        instant = _utc_now(now)
        fingerprint = _fingerprint(
            "client_status",
            session_id,
            page_visibility.value if page_visibility else "",
            connectivity_status.value if connectivity_status else "",
            audio_status.value if audio_status else "",
            gps_status.value if gps_status else "",
            power_status.value if power_status else "",
            source,
        )
        async with self._lock:
            session = self._get_authorized(session_id, token, instant)
            if session.state is SessionState.CANCELLED:
                raise ConflictError("SESSION_CANCELLED", "A cancelled session cannot be updated")
            duplicate = self._find_duplicate(event_id, fingerprint)
            if duplicate is not None:
                return CommandResult(copy.deepcopy(session), duplicate=True)

            changed = False
            transitions: list[SafetyTransition] = []
            if page_visibility is not None:
                changed |= session.page_visibility is not page_visibility
                session.page_visibility = page_visibility
                transitions.append(
                    self._set_issue_locked(
                        session,
                        TechnicalIssue.PAGE_HIDDEN,
                        active=page_visibility is PageVisibility.HIDDEN,
                        now=instant,
                    )
                )
            if connectivity_status is not None:
                changed |= session.connectivity_status is not connectivity_status
                session.connectivity_status = connectivity_status
                if connectivity_status is not ConnectivityStatus.UNKNOWN:
                    transitions.append(
                        self._set_issue_locked(
                            session,
                            TechnicalIssue.NETWORK_DISCONNECTED,
                            active=connectivity_status is ConnectivityStatus.DISCONNECTED,
                            now=instant,
                        )
                    )
            if audio_status is not None:
                changed |= session.audio_status is not audio_status
                session.audio_status = audio_status
                if audio_status is not AudioStatus.UNKNOWN:
                    transitions.append(
                        self._set_issue_locked(
                            session,
                            TechnicalIssue.AUDIO_DISCONNECTED,
                            active=audio_status is AudioStatus.DISCONNECTED,
                            now=instant,
                        )
                    )
                    transitions.append(
                        self._set_issue_locked(
                            session,
                            TechnicalIssue.AUDIO_INTERRUPTED,
                            active=audio_status is AudioStatus.ERROR,
                            now=instant,
                        )
                    )
            if gps_status is not None:
                changed |= session.gps_status is not gps_status
                session.gps_status = gps_status
                transitions.extend(
                    [
                        self._set_issue_locked(
                            session,
                            TechnicalIssue.GPS_STALE,
                            active=gps_status is GpsStatus.GPS_STALE,
                            now=instant,
                        ),
                        self._set_issue_locked(
                            session,
                            TechnicalIssue.GPS_UNAVAILABLE,
                            active=gps_status is GpsStatus.GPS_UNAVAILABLE,
                            now=instant,
                        ),
                    ]
                )
            if power_status is not None:
                changed |= session.power_status is not power_status
                session.power_status = power_status
                transitions.extend(
                    [
                        self._set_issue_locked(
                            session,
                            TechnicalIssue.POWER_UNSTABLE,
                            active=power_status is PowerStatus.POWER_UNSTABLE,
                            now=instant,
                        ),
                        self._set_issue_locked(
                            session,
                            TechnicalIssue.LOW_VOLTAGE,
                            active=power_status is PowerStatus.LOW_VOLTAGE,
                            now=instant,
                        ),
                    ]
                )

            effective = [t for t in transitions if t.kind is not TransitionKind.NO_CHANGE]
            if changed and not effective:
                session.updated_at = instant
                session.version += 1
            for transition in effective:
                await self._handle_transition(
                    session,
                    transition,
                    extra={"source": source},
                    command_event_id=event_id,
                )
            self._remember(event_id, fingerprint, session_id)
            await self._publish_client_status(session, source=source)
            return CommandResult(copy.deepcopy(session))

    async def set_voice_status(
        self,
        session_id: str,
        *,
        token: str,
        event_id: str,
        status: VoiceConnectionStatus,
        now: datetime | None = None,
    ) -> CommandResult:
        instant = _utc_now(now)
        fingerprint = _fingerprint("voice_status", session_id, status.value)
        async with self._lock:
            session = self._get_authorized(session_id, token, instant)
            if session.state is SessionState.CANCELLED:
                raise ConflictError("SESSION_CANCELLED", "A cancelled voice session cannot update")
            duplicate = self._find_duplicate(event_id, fingerprint)
            if duplicate is not None:
                return CommandResult(copy.deepcopy(session), duplicate=True)

            issue_targets: dict[TechnicalIssue, bool]
            all_voice_issues = (
                TechnicalIssue.VOICE_UNAVAILABLE,
                TechnicalIssue.VOICE_TIMEOUT,
                TechnicalIssue.MICROPHONE_DENIED,
                TechnicalIssue.AUDIO_DISCONNECTED,
                TechnicalIssue.AUDIO_INTERRUPTED,
            )
            if status in {
                VoiceConnectionStatus.CONNECTED,
                VoiceConnectionStatus.RECONNECTED,
            }:
                session.audio_status = AudioStatus.CONNECTED
                active_issue: TechnicalIssue | None = None
            elif status is VoiceConnectionStatus.DISCONNECTED:
                session.audio_status = AudioStatus.DISCONNECTED
                active_issue = TechnicalIssue.AUDIO_DISCONNECTED
            elif status is VoiceConnectionStatus.MICROPHONE_ERROR:
                session.audio_status = AudioStatus.ERROR
                active_issue = TechnicalIssue.MICROPHONE_DENIED
            elif status is VoiceConnectionStatus.TIMEOUT:
                session.audio_status = AudioStatus.ERROR
                active_issue = TechnicalIssue.VOICE_TIMEOUT
            elif status is VoiceConnectionStatus.RECONNECTING:
                session.audio_status = AudioStatus.UNKNOWN
                active_issue = TechnicalIssue.VOICE_UNAVAILABLE
            else:
                session.audio_status = AudioStatus.ERROR
                active_issue = TechnicalIssue.VOICE_UNAVAILABLE

            issue_targets = {issue: issue is active_issue for issue in all_voice_issues}

            effective: list[SafetyTransition] = []
            for issue, active in issue_targets.items():
                transition = self._set_issue_locked(session, issue, active=active, now=instant)
                if transition.kind is not TransitionKind.NO_CHANGE:
                    effective.append(transition)
                    await self._handle_transition(
                        session,
                        transition,
                        extra={"source": "voice_gateway"},
                        command_event_id=event_id,
                    )
            if not effective:
                session.updated_at = instant
                session.version += 1
            self._remember(event_id, fingerprint, session_id)
            await self._broker.publish(
                "voice.status_changed",
                session_id=session.session_id,
                device_id=session.device_id,
                data={
                    "status": status.value,
                    "health_status": session.health_status.value,
                    "state": session.state.value,
                    "version": session.version,
                },
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
            if not trusted_internal:
                if token is None:
                    raise AuthenticationError()
                self._verify_token(token, session_id, instant)
            session = self._get(session_id)
            if trusted_internal and session.state is SessionState.CANCELLED:
                return CommandResult(copy.deepcopy(session))
            if not trusted_internal:
                duplicate = self._find_duplicate(event_id, fingerprint)
                if duplicate is not None:
                    return CommandResult(copy.deepcopy(session), duplicate=True)
            try:
                transition = self._set_issue_locked(
                    session,
                    issue,
                    active=active,
                    now=instant,
                )
            except InvalidTransition as exc:
                raise _transition_conflict(exc) from exc
            if not trusted_internal:
                self._remember(event_id, fingerprint, session_id)
            await self._handle_transition(
                session, transition, extra={"source": source}, command_event_id=event_id
            )
            return CommandResult(copy.deepcopy(session))

    async def recover_unfinished(
        self,
        *,
        now: datetime | None = None,
    ) -> list[SafetySession]:
        """Recover persistent sessions without creating replacement sessions."""

        instant = _utc_now(now)
        recovered_items = await self._recovery_provider.load_unfinished_sessions()
        bundles = [
            item
            if isinstance(item, RecoveredSessionBundle)
            else RecoveredSessionBundle(session=item)
            for item in recovered_items
        ]
        unfinished = [
            bundle
            for bundle in bundles
            if bundle.session.state
            in {
                SessionState.ACTIVE,
                SessionState.CHECK_IN,
                SessionState.UNCONFIRMED,
                SessionState.ALERT,
            }
        ]
        if len(unfinished) > 1:
            raise RuntimeError(
                "Recovery provider returned multiple unfinished sessions; "
                "the GPIO deployment permits exactly one"
            )
        accepted: list[SafetySession] = []
        async with self._lock:
            for bundle in unfinished:
                candidate = bundle.session
                if candidate.session_id in self._sessions:
                    continue
                session = copy.deepcopy(candidate)
                session.audio_status = AudioStatus.DISCONNECTED
                session.technical_issues.update(
                    {
                        TechnicalIssue.PROCESS_RESTARTED,
                        TechnicalIssue.AUDIO_DISCONNECTED,
                        TechnicalIssue.VOICE_UNAVAILABLE,
                    }
                )
                session.updated_at = instant
                session.version += 1
                alert: AlertRecord | None = None
                bundled_commands: dict[str, RecoveredCommand] = {}
                for command in bundle.processed_commands:
                    self._validate_recovered_command(
                        command,
                        expected_session_id=session.session_id,
                    )
                    prior = bundled_commands.get(command.event_id)
                    if prior is not None and (
                        prior.fingerprint != command.fingerprint
                        or prior.session_id != command.session_id
                    ):
                        raise RuntimeError("Recovered idempotency records conflict")
                    bundled_commands[command.event_id] = command
                if bundle.alert is not None:
                    if bundle.alert.session_id != session.session_id:
                        raise RuntimeError("Recovered alert belongs to another session")
                    if (
                        session.state is not SessionState.ALERT
                        or AlertStatus(bundle.alert.status) is not AlertStatus.ACTIVE
                    ):
                        raise RuntimeError("Recovered alert and safety state are inconsistent")
                    alert = self._alerts.restore(bundle.alert)
                elif session.state is SessionState.ALERT:
                    alert, _ = self._alerts.ensure_active(
                        session.session_id,
                        _incident_for_session(session),
                        now=instant,
                    )
                for command in bundle.processed_commands:
                    self._restore_command(command, expected_session_id=session.session_id)
                self._sessions[session.session_id] = session
                await self._broker.publish(
                    "session.recovered",
                    session_id=session.session_id,
                    device_id=session.device_id,
                    data={
                        "state": session.state.value,
                        "health_status": session.health_status.value,
                        "version": session.version,
                        "alert_id": alert.alert_id if alert else None,
                        "audio_recovered": False,
                    },
                )
                accepted.append(copy.deepcopy(session))
        return accepted

    def _restore_command(
        self,
        command: RecoveredCommand,
        *,
        expected_session_id: str,
    ) -> None:
        self._validate_recovered_command(
            command,
            expected_session_id=expected_session_id,
        )
        self._processed_commands[command.event_id] = _ProcessedCommand(
            command.fingerprint,
            command.session_id,
        )
        self._processed_commands.move_to_end(command.event_id)
        if len(self._processed_commands) > self._max_processed_commands:
            self._processed_commands.popitem(last=False)

    def _validate_recovered_command(
        self,
        command: RecoveredCommand,
        *,
        expected_session_id: str,
    ) -> None:
        if command.session_id != expected_session_id:
            raise RuntimeError("Recovered command belongs to another session")
        if not command.event_id or len(command.event_id) > 128:
            raise RuntimeError("Recovered command has an invalid event_id")
        if len(command.fingerprint) != 64:
            raise RuntimeError("Recovered command has an invalid fingerprint")
        try:
            int(command.fingerprint, 16)
        except ValueError as exc:
            raise RuntimeError("Recovered command has an invalid fingerprint") from exc
        existing = self._processed_commands.get(command.event_id)
        if existing is not None and (
            existing.fingerprint != command.fingerprint or existing.session_id != command.session_id
        ):
            raise RuntimeError("Recovered idempotency records conflict")

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
            self._verify_token(token, session_id, instant)
            return copy.deepcopy(self._get(session_id))

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
                    await self._handle_transition(
                        session,
                        connection_transition,
                        extra={"source": "heartbeat_timeout"},
                    )
                    await self._publish_client_status(session, source="heartbeat_timeout")
                transition = self._engine.advance(session, now=instant)
                if transition.kind is not TransitionKind.NO_CHANGE:
                    transitions.append(transition)
                    await self._handle_transition(session, transition)
                    if transition.kind is TransitionKind.CHECK_IN_UNDELIVERED:
                        issue_transition = self._set_issue_locked(
                            session,
                            TechnicalIssue.AUDIO_DISCONNECTED,
                            active=True,
                            now=instant,
                        )
                        session.audio_status = AudioStatus.DISCONNECTED
                        await self._handle_transition(
                            session,
                            issue_transition,
                            extra={"source": "checkin_delivery_timeout"},
                        )
        return transitions

    async def run_scheduler(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.advance_all(now=datetime.now(UTC))
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad persistence/integration record must never silently
                # disable every server-side safety timer.
                LOGGER.exception("safety scheduler tick failed; retrying")
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

    async def _resolve_alert_locked(
        self,
        session: SafetySession,
        *,
        alert_id: str | None,
        now: datetime,
        action: str,
        cancellation_reason: str | None = None,
        command_event_id: str | None = None,
    ) -> AlertCommandResult:
        current = self._alerts.get_for_session(session.session_id)
        if current is None:
            raise NotFoundError("Alert not found for this session")
        selected_id = alert_id or current.alert_id
        try:
            alert, changed = self._alerts.resolve(
                session.session_id,
                selected_id,
                now=now,
                action=action,
            )
        except AlertNotFound as exc:
            raise NotFoundError("Alert not found") from exc
        except AlertSessionMismatch as exc:
            raise AuthenticationError("Alert is not authorized for this session") from exc
        except AlertRegistryError as exc:
            raise ConflictError("ALERT_RESOLUTION_FAILED", str(exc)) from exc

        if not changed:
            if session.state is SessionState.CANCELLED:
                return AlertCommandResult(copy.deepcopy(session), duplicate=True, alert=alert)
            raise ConflictError("ALERT_ALREADY_RESOLVED", "The alert is already resolved")
        try:
            transition = self._engine.resolve_alert(session, now=now)
        except InvalidTransition as exc:
            raise _transition_conflict(exc) from exc
        if cancellation_reason is not None:
            session.cancellation_reason = cancellation_reason
            transition.details["reason"] = cancellation_reason
        await self._handle_transition(
            session,
            transition,
            extra={
                "alert_id": alert.alert_id,
                "alert_status": alert.status.value,
                "resolution_action": alert.resolution_action,
                "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
            },
            command_event_id=command_event_id,
            reason=cancellation_reason or action,
        )
        await self._broker.publish(
            "alert.resolved",
            session_id=session.session_id,
            device_id=session.device_id,
            data={
                "alert_id": alert.alert_id,
                "status": alert.status.value,
                "resolution_action": alert.resolution_action,
                "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
                "notify_dashboard": True,
                "notify_trusted_contact": True,
                "version": session.version,
            },
        )
        return AlertCommandResult(copy.deepcopy(session), alert=alert)

    def _expire_connectivity_lease(
        self,
        session: SafetySession,
        now: datetime,
    ) -> SafetyTransition | None:
        lease = session.connection_lease_expires_at
        if (
            lease is None
            or now < lease
            or session.state is SessionState.CANCELLED
            or TechnicalIssue.NETWORK_DISCONNECTED in session.technical_issues
        ):
            return None
        session.connectivity_status = ConnectivityStatus.DISCONNECTED
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

    def _current_unfinished(self) -> SafetySession | None:
        unfinished = [
            session
            for session in self._sessions.values()
            if session.state
            in {
                SessionState.ACTIVE,
                SessionState.CHECK_IN,
                SessionState.UNCONFIRMED,
                SessionState.ALERT,
            }
        ]
        if not unfinished:
            return None
        priority = {
            SessionState.ALERT: 4,
            SessionState.UNCONFIRMED: 3,
            SessionState.CHECK_IN: 2,
            SessionState.ACTIVE: 1,
        }
        return max(unfinished, key=lambda item: (priority[item.state], item.version))

    def _get_authorized(self, session_id: str, token: str, now: datetime) -> SafetySession:
        self._verify_token(token, session_id, now)
        return self._get(session_id)

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

    def _set_issue_locked(
        self,
        session: SafetySession,
        issue: TechnicalIssue,
        *,
        active: bool,
        now: datetime,
    ) -> SafetyTransition:
        if active:
            return self._engine.report_technical_issue(session, issue, now=now)
        return self._engine.resolve_technical_issue(session, issue, now=now)

    async def _handle_transition(
        self,
        session: SafetySession,
        transition: SafetyTransition,
        *,
        extra: dict[str, Any] | None = None,
        command_event_id: str | None = None,
        reason: str | None = None,
    ) -> AlertRecord | None:
        if transition.kind is TransitionKind.NO_CHANGE:
            return self._alerts.get_for_session(session.session_id)
        enriched = dict(extra or {})
        alert: AlertRecord | None = None
        created = False
        if transition.kind is TransitionKind.ALERTED:
            alert, created = self._alerts.ensure_active(
                session.session_id,
                _incident_for_session(session),
                now=transition.occurred_at,
            )
            enriched.update(
                {
                    "alert_id": alert.alert_id,
                    "alert_status": alert.status.value,
                }
            )
        await self._publish_transition(
            session,
            transition,
            extra=enriched,
            command_event_id=command_event_id,
            reason=reason,
        )
        if alert is not None and created:
            await self._publish_notification_request(session, alert)
        return alert

    async def _publish_transition(
        self,
        session: SafetySession,
        transition: SafetyTransition,
        *,
        extra: dict[str, Any] | None = None,
        command_event_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        if transition.kind is TransitionKind.NO_CHANGE:
            return
        event_type = _EVENT_TYPES[transition.kind]
        data: dict[str, Any] = {
            "from_state": transition.from_state.value,
            "state": transition.to_state.value,
            "health_status": session.health_status.value,
            "version": transition.version,
            **transition.details,
        }
        if transition.checkin_id is not None:
            data.setdefault("checkin_id", transition.checkin_id)
        if extra:
            data.update({key: value for key, value in extra.items() if value is not None})
        await self._broker.publish(
            event_type,
            session_id=session.session_id,
            device_id=session.device_id,
            data=data,
        )
        # Persistența (Tiger Data) e asincronă și nu blochează sau modifică decizia de siguranță.
        self._recorder.submit(
            TransitionRecord(
                session_id=session.session_id,
                device_id=session.device_id,
                kind=transition.kind.value,
                from_state=transition.from_state.value,
                to_state=transition.to_state.value,
                occurred_at=transition.occurred_at,
                details=dict(data),
                source=(extra or {}).get("source"),
                command_event_id=command_event_id,
                reason=reason,
            )
        )

    async def _publish_notification_request(
        self,
        session: SafetySession,
        alert: AlertRecord,
    ) -> None:
        location: AuthorizedLocation | None = None
        try:
            location = await asyncio.wait_for(
                self._location_provider.get_last_authorized(session.session_id),
                timeout=0.25,
            )
        except TimeoutError:
            LOGGER.warning(
                "authorized location lookup timed out",
                extra={"session_id": session.session_id},
            )
        except Exception:
            LOGGER.exception(
                "authorized location lookup failed",
                extra={"session_id": session.session_id},
            )
        location_payload: dict[str, Any] | None = None
        if location is not None:
            age_seconds = max(
                0.0,
                (alert.notification_requested_at - location.captured_at).total_seconds(),
            )
            location_payload = {
                "latitude": location.latitude,
                "longitude": location.longitude,
                "captured_at": location.captured_at.isoformat(),
                "age_seconds": age_seconds,
                "is_stale": session.gps_status is not GpsStatus.GPS_AVAILABLE,
                "accuracy_meters": location.accuracy_meters,
                "source": location.source,
            }
        await self._broker.publish(
            "alert.notification_requested",
            session_id=session.session_id,
            device_id=session.device_id,
            data={
                "alert_id": alert.alert_id,
                "incident_type": alert.incident_type.value,
                "notification_requested_at": alert.notification_requested_at.isoformat(),
                "gps_status": session.gps_status.value,
                "location": location_payload,
                "location_available": location_payload is not None,
                "version": session.version,
            },
        )

    async def _publish_client_status(self, session: SafetySession, *, source: str) -> None:
        await self._broker.publish(
            "client.status_changed",
            session_id=session.session_id,
            device_id=session.device_id,
            data={
                "page_visibility": session.page_visibility.value,
                "connectivity_status": session.connectivity_status.value,
                "audio_status": session.audio_status.value,
                "gps_status": session.gps_status.value,
                "power_status": session.power_status.value,
                "health_status": session.health_status.value,
                "state": session.state.value,
                "version": session.version,
                "source": source,
            },
        )


def _incident_for_session(session: SafetySession) -> AlertIncidentType:
    if session.alert_reason is AlertReason.SOS_EXPLICIT:
        return AlertIncidentType.EXPLICIT_SOS
    return AlertIncidentType.MISSED_CHECK_INS


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
    return ConflictError("INVALID_SESSION_TRANSITION", str(exc))


def _checkin_conflict(
    exc: InvalidCheckIn,
    session: SafetySession,
    checkin_id: str,
) -> ConflictError:
    message = str(exc)
    if session.current_checkin_id != checkin_id and "expired" not in message.lower():
        return ConflictError("CHECKIN_MISMATCH", message)
    if "expired" in message.lower() or session.state is not SessionState.CHECK_IN:
        return ConflictError("CHECKIN_EXPIRED", message)
    return ConflictError("CHECKIN_INVALID", message)


def _checkin_state_conflict(
    exc: InvalidTransition,
    session: SafetySession,
) -> ConflictError:
    if session.state in {
        SessionState.ACTIVE,
        SessionState.UNCONFIRMED,
        SessionState.ALERT,
        SessionState.CANCELLED,
    }:
        return ConflictError("CHECKIN_EXPIRED", str(exc))
    return _transition_conflict(exc)
