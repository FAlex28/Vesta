from __future__ import annotations

import asyncio
import hmac
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import (
    APIRouter,
    Header,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from app.container import AppServices
from app.errors import AuthenticationError, ConflictError, ServiceUnavailableError
from app.safety import (
    AudioStatus,
    ConnectivityStatus,
    GpsStatus,
    HealthStatus,
    PageVisibility,
    PowerStatus,
    SafetySession,
    SessionState,
    TechnicalIssue,
)
from app.schemas import (
    ActivationResponse,
    AlertView,
    CancelRequest,
    CheckInDeliveryRequest,
    CheckInRequest,
    ClientStatusRequest,
    CommandResponse,
    DeviceEventRequest,
    HeartbeatRequest,
    LocationUpdateRequest,
    LocationUpdateResponse,
    LocationView,
    ResolveAlertRequest,
    SessionView,
    SosRequest,
    SystemStatusResponse,
    TechnicalStatusRequest,
    VoiceAccessRequest,
    VoiceAccessResponse,
    VoiceStatusRequest,
)
from app.services.alerts import AlertRecord
from app.services.location import LocationEventReused, LocationFromFuture
from app.services.session_manager import ActivationOutcome
from app.voice import (
    VoiceConnectionStatus,
    VoiceConversationAlreadyActive,
    VoiceConversationError,
    VoiceProviderError,
    VoiceRetryLater,
)

router = APIRouter()


def _services(request: Request) -> AppServices:
    return request.app.state.services


def _bearer(authorization: str | None) -> str:
    if not authorization:
        raise AuthenticationError()
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError()
    return token.strip()


def _require_dashboard_key(services: AppServices, supplied: str | None) -> None:
    expected = services.settings.dashboard_api_key
    # Development/test can deliberately run without a shared dashboard key.
    # Settings rejects this configuration for every deployed environment.
    if expected is None and services.settings.environment in {"development", "test"}:
        return
    if expected is None or supplied is None or not hmac.compare_digest(expected, supplied):
        raise AuthenticationError("Invalid or missing dashboard key")


def _alert_view(alert: AlertRecord | None) -> AlertView | None:
    if alert is None:
        return None
    return AlertView(
        id=alert.alert_id,
        incident_type=alert.incident_type.value,
        status=alert.status.value,
        created_at=alert.created_at,
        updated_at=alert.updated_at,
        notification_requested_at=alert.notification_requested_at,
        resolved_at=alert.resolved_at,
        resolution_action=alert.resolution_action,
        version=alert.version,
    )


def _session_view(session: SafetySession, services: AppServices) -> SessionView:
    if session.created_at is None or session.updated_at is None:
        raise RuntimeError("An activated session must have timestamps")
    return SessionView(
        id=session.session_id,
        device_id=session.device_id,
        state=session.state.value,
        health_status=session.health_status.value,
        version=session.version,
        created_at=session.created_at,
        updated_at=session.updated_at,
        quick_cancel_until=session.quick_cancel_until,
        cancellation_reason=session.cancellation_reason,
        checkin_attempt=session.current_attempt,
        missed_attempts=session.missed_attempts,
        undelivered_attempts=session.undelivered_attempts,
        checkin_id=session.current_checkin_id,
        checkin_status=(
            session.current_checkin_status.value if session.current_checkin_status else None
        ),
        checkin_requested_at=session.current_checkin_requested_at,
        checkin_delivered_at=session.current_checkin_delivered_at,
        last_checkin_id=session.last_checkin_id,
        last_checkin_status=(
            session.last_checkin_status.value if session.last_checkin_status else None
        ),
        last_confirmation_at=session.last_confirmation_at,
        next_checkin_at=session.next_check_in_at,
        response_deadline=session.response_deadline,
        retry_at=session.retry_at,
        client_last_seen_at=session.client_last_seen_at,
        connection_lease_expires_at=session.connection_lease_expires_at,
        page_visibility=session.page_visibility.value,
        connectivity_status=session.connectivity_status.value,
        audio_status=session.audio_status.value,
        gps_status=session.gps_status.value,
        power_status=session.power_status.value,
        active_technical_issues=sorted(issue.value for issue in session.technical_issues),
        alert_reason=session.alert_reason.value if session.alert_reason else None,
        alert=_alert_view(services.alerts.get_for_session(session.session_id)),
    )


def _system_health(sessions: list[SafetySession]) -> HealthStatus:
    statuses = {session.health_status for session in sessions}
    if HealthStatus.DISCONNECTED in statuses:
        return HealthStatus.DISCONNECTED
    if HealthStatus.DEGRADED in statuses:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


async def _status(
    services: AppServices,
    *,
    session_id: str | None = None,
) -> SystemStatusResponse:
    snapshot = await services.sessions.snapshot()
    sessions = snapshot.sessions
    if session_id is not None:
        sessions = [session for session in sessions if session.session_id == session_id]
    return SystemStatusResponse(
        system_state=(
            snapshot.state.value
            if session_id is None
            else (
                sessions[0].state.value
                if sessions and sessions[0].state is not SessionState.CANCELLED
                else SessionState.IDLE.value
            )
        ),
        system_health=_system_health(sessions).value,
        generated_at=datetime.now(UTC),
        latest_sequence=snapshot.latest_sequence,
        sessions=[_session_view(session, services) for session in sessions],
        voice_mode=services.settings.voice_mode,
    )


@router.get("/health", tags=["system"])
async def health(request: Request) -> dict[str, str]:
    services = _services(request)
    voice_configured = bool(
        services.settings.voice_mode == "demo"
        or (services.settings.elevenlabs_api_key and services.settings.elevenlabs_agent_id)
    )
    return {
        "status": "ok" if voice_configured else "degraded",
        "service": "vesta-backend",
        "voice_mode": services.settings.voice_mode,
    }


@router.get(
    "/api/v1/status",
    response_model=SystemStatusResponse,
    tags=["system"],
)
async def system_status(
    request: Request,
    x_dashboard_key: Annotated[str | None, Header()] = None,
) -> SystemStatusResponse:
    services = _services(request)
    _require_dashboard_key(services, x_dashboard_key)
    return await _status(services)


@router.post(
    "/api/v1/device/events",
    response_model=ActivationResponse,
    tags=["device"],
)
async def device_event(
    payload: DeviceEventRequest,
    request: Request,
    response: Response,
    x_device_key: Annotated[str | None, Header()] = None,
) -> ActivationResponse:
    services = _services(request)
    expected_key = services.settings.device_api_key
    if expected_key is not None and (
        x_device_key is None or not hmac.compare_digest(expected_key, x_device_key)
    ):
        raise AuthenticationError("Invalid or missing device key")
    result = await services.sessions.activate(
        event_id=payload.event_id,
        device_id=payload.device_id,
        now=payload.occurred_at,
    )
    response.status_code = (
        status.HTTP_201_CREATED
        if result.outcome is ActivationOutcome.ACTIVATED
        else status.HTTP_200_OK
    )
    response.headers["Cache-Control"] = "no-store"
    if result.control_token_expires_at is None:
        raise RuntimeError("Activation token is missing an expiry")
    return ActivationResponse(
        outcome=result.outcome.value,
        duplicate=result.duplicate,
        session=_session_view(result.session, services),
        control_token=result.control_token,
        control_token_expires_at=result.control_token_expires_at,
    )


@router.get(
    "/api/v1/sessions/{session_id}",
    response_model=SessionView,
    tags=["sessions"],
)
async def get_session(
    session_id: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> SessionView:
    services = _services(request)
    session = await services.sessions.authorize(session_id, _bearer(authorization))
    return _session_view(session, services)


@router.post(
    "/api/v1/sessions/{session_id}/checkins",
    response_model=CommandResponse,
    tags=["sessions"],
)
async def confirm_checkin(
    session_id: str,
    payload: CheckInRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CommandResponse:
    services = _services(request)
    result = await services.sessions.confirm_safe(
        session_id,
        token=_bearer(authorization),
        event_id=payload.event_id,
        checkin_id=payload.checkin_id,
        source=payload.source,
    )
    return CommandResponse(
        duplicate=result.duplicate,
        session=_session_view(result.session, services),
    )


@router.post(
    "/api/v1/sessions/{session_id}/checkins/{checkin_id}/delivery",
    response_model=CommandResponse,
    tags=["sessions"],
)
async def report_checkin_delivery(
    session_id: str,
    checkin_id: str,
    payload: CheckInDeliveryRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CommandResponse:
    services = _services(request)
    result = await services.sessions.mark_checkin_delivery(
        session_id,
        checkin_id,
        token=_bearer(authorization),
        event_id=payload.event_id,
        delivered=payload.status == "DELIVERED",
        channel=payload.channel,
    )
    return CommandResponse(
        duplicate=result.duplicate,
        session=_session_view(result.session, services),
    )


@router.post(
    "/api/v1/sessions/{session_id}/sos",
    response_model=CommandResponse,
    tags=["sessions"],
)
async def request_sos(
    session_id: str,
    payload: SosRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CommandResponse:
    services = _services(request)
    result = await services.sessions.request_sos(
        session_id,
        token=_bearer(authorization),
        event_id=payload.event_id,
        source=payload.source,
        reason=payload.reason,
    )
    return CommandResponse(
        duplicate=result.duplicate,
        session=_session_view(result.session, services),
    )


@router.post(
    "/api/v1/sessions/{session_id}/cancel",
    response_model=CommandResponse,
    tags=["sessions"],
)
async def cancel_session(
    session_id: str,
    payload: CancelRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CommandResponse:
    services = _services(request)
    token = _bearer(authorization)
    result = await services.sessions.cancel(
        session_id,
        token=token,
        event_id=payload.event_id,
        reason=payload.reason,
    )
    await services.voice_sessions.update(
        session_id,
        VoiceConnectionStatus.DISCONNECTED,
        now=datetime.now(UTC),
    )
    return CommandResponse(
        duplicate=result.duplicate,
        session=_session_view(result.session, services),
    )


@router.post(
    "/api/v1/sessions/{session_id}/alerts/{alert_id}/resolve",
    response_model=CommandResponse,
    tags=["alerts"],
)
async def resolve_alert(
    session_id: str,
    alert_id: str,
    payload: ResolveAlertRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CommandResponse:
    services = _services(request)
    result = await services.sessions.resolve_alert(
        session_id,
        alert_id,
        token=_bearer(authorization),
        event_id=payload.event_id,
        action=payload.action,
    )
    await services.voice_sessions.update(
        session_id,
        VoiceConnectionStatus.DISCONNECTED,
        now=datetime.now(UTC),
    )
    return CommandResponse(
        duplicate=result.duplicate,
        session=_session_view(result.session, services),
    )


@router.post(
    "/api/v1/sessions/{session_id}/heartbeat",
    response_model=CommandResponse,
    tags=["sessions"],
)
async def session_heartbeat(
    session_id: str,
    payload: HeartbeatRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CommandResponse:
    services = _services(request)
    result = await services.sessions.heartbeat(
        session_id,
        token=_bearer(authorization),
        page_visibility=(
            PageVisibility(payload.page_visibility) if payload.page_visibility else None
        ),
    )
    return CommandResponse(
        duplicate=False,
        session=_session_view(result.session, services),
    )


@router.post(
    "/api/v1/sessions/{session_id}/client-status",
    response_model=CommandResponse,
    tags=["sessions"],
)
async def report_client_status(
    session_id: str,
    payload: ClientStatusRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CommandResponse:
    services = _services(request)
    result = await services.sessions.update_client_status(
        session_id,
        token=_bearer(authorization),
        event_id=payload.event_id,
        page_visibility=(
            PageVisibility(payload.page_visibility) if payload.page_visibility else None
        ),
        connectivity_status=(
            ConnectivityStatus(payload.connectivity_status) if payload.connectivity_status else None
        ),
        audio_status=AudioStatus(payload.audio_status) if payload.audio_status else None,
        gps_status=GpsStatus(payload.gps_status) if payload.gps_status else None,
        power_status=PowerStatus(payload.power_status) if payload.power_status else None,
    )
    return CommandResponse(
        duplicate=result.duplicate,
        session=_session_view(result.session, services),
    )


@router.post(
    "/api/v1/sessions/{session_id}/location",
    response_model=LocationUpdateResponse,
    tags=["sessions"],
)
async def record_authorized_location(
    session_id: str,
    payload: LocationUpdateRequest,
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
) -> LocationUpdateResponse:
    services = _services(request)
    await services.sessions.authorize(session_id, _bearer(authorization))
    try:
        result = await services.locations.record(
            event_id=payload.event_id,
            session_id=session_id,
            latitude=payload.latitude,
            longitude=payload.longitude,
            captured_at=payload.captured_at,
            received_at=datetime.now(UTC),
            accuracy_meters=payload.accuracy_meters,
            source=payload.source,
        )
    except LocationEventReused as exc:
        raise ConflictError("EVENT_ID_REUSED", str(exc)) from exc
    except LocationFromFuture as exc:
        raise ConflictError("LOCATION_TIMESTAMP_INVALID", str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    location = result.location
    return LocationUpdateResponse(
        duplicate=result.duplicate,
        accepted_as_latest=result.accepted_as_latest,
        location=LocationView(
            latitude=location.latitude,
            longitude=location.longitude,
            captured_at=location.captured_at,
            received_at=location.received_at,
            accuracy_meters=location.accuracy_meters,
            source=location.source,
        ),
    )


@router.post(
    "/api/v1/sessions/{session_id}/technical-status",
    response_model=CommandResponse,
    tags=["sessions"],
)
async def report_technical_status(
    session_id: str,
    payload: TechnicalStatusRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CommandResponse:
    services = _services(request)
    result = await services.sessions.set_technical_issue(
        session_id,
        token=_bearer(authorization),
        event_id=payload.event_id,
        issue=TechnicalIssue(payload.issue),
        active=payload.active,
        source=payload.source,
    )
    return CommandResponse(
        duplicate=result.duplicate,
        session=_session_view(result.session, services),
    )


@router.post(
    "/api/v1/sessions/{session_id}/voice/access",
    response_model=VoiceAccessResponse,
    tags=["voice"],
)
async def create_voice_access(
    session_id: str,
    payload: VoiceAccessRequest,
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
) -> VoiceAccessResponse:
    services = _services(request)
    token = _bearer(authorization)
    session = await services.sessions.authorize(session_id, token)
    if session.state is SessionState.CANCELLED:
        raise ConflictError("SESSION_CANCELLED", "A cancelled session cannot start voice")
    now = datetime.now(UTC)
    try:
        await services.voice_sessions.reserve(session_id, now=now)
    except VoiceConversationAlreadyActive as exc:
        raise ConflictError("VOICE_ALREADY_ACTIVE", str(exc)) from exc
    except VoiceRetryLater as exc:
        raise ServiceUnavailableError(
            "VOICE_RETRY_LATER",
            f"Voice retry is deferred until {exc.retry_after.isoformat()}",
        ) from exc

    try:
        access = await services.voice.create_access(
            session_id=session_id,
            participant_name=payload.participant_name,
            environment=payload.environment,
        )
    except VoiceProviderError as exc:
        record = await services.voice_sessions.provider_failed(
            session_id,
            now=datetime.now(UTC),
            retryable=exc.retryable,
        )
        await services.sessions.set_voice_status(
            session_id,
            token=token,
            event_id=f"internal-voice-error-{uuid4()}",
            status=record.status,
        )
        raise ServiceUnavailableError(f"VOICE_{exc.code.value.upper()}", str(exc)) from exc

    current_session = await services.sessions.authorize(session_id, token)
    if current_session.state is SessionState.CANCELLED:
        await services.voice_sessions.update(
            session_id,
            VoiceConnectionStatus.DISCONNECTED,
            now=datetime.now(UTC),
        )
        raise ConflictError("SESSION_CANCELLED", "A cancelled session cannot start voice")
    conversation_id = access.conversation_id or f"voice-{uuid4()}"
    await services.voice_sessions.token_issued(
        session_id,
        conversation_id,
        now=datetime.now(UTC),
    )
    await services.sessions.set_technical_issue(
        session_id,
        issue=TechnicalIssue.VOICE_UNAVAILABLE,
        active=False,
        event_id=f"internal-voice-access-{uuid4()}",
        source="voice_gateway",
        trusted_internal=True,
    )
    if access.provider == "demo":
        await services.voice_sessions.update(
            session_id,
            VoiceConnectionStatus.CONNECTED,
            conversation_id=conversation_id,
            now=datetime.now(UTC),
        )

    public = access.to_public_dict()
    client_config = {
        key: value
        for key, value in public.items()
        if key
        not in {
            "provider",
            "mode",
            "transport",
            "conversation_token",
            "conversation_id",
        }
    }
    client_config.update(
        {
            "session_id": session_id,
            "safety_event": "safety.check_in_requested",
            "contextual_update": (
                "La evenimentul safety.check_in_requested, trimite agentului contextul "
                "safety_check_requested. Acțiunile explicite se confirmă prin API-ul Vesta."
            ),
        }
    )
    current_session = await services.sessions.authorize(session_id, token)
    if current_session.state is SessionState.CANCELLED:
        await services.voice_sessions.update(
            session_id,
            VoiceConnectionStatus.DISCONNECTED,
            now=datetime.now(UTC),
        )
        raise ConflictError("SESSION_CANCELLED", "A cancelled session cannot start voice")
    response.headers["Cache-Control"] = "no-store"
    return VoiceAccessResponse(
        provider=public["provider"],
        mode=public["mode"],
        transport=public["transport"],
        conversation_token=public.get("conversation_token"),
        conversation_id=public.get("conversation_id"),
        client_config=client_config,
    )


@router.post(
    "/api/v1/sessions/{session_id}/voice/status",
    response_model=CommandResponse,
    tags=["voice"],
)
async def report_voice_status(
    session_id: str,
    payload: VoiceStatusRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CommandResponse:
    services = _services(request)
    token = _bearer(authorization)
    await services.sessions.authorize(session_id, token)
    voice_status = VoiceConnectionStatus(payload.status)
    current_voice = await services.voice_sessions.get(session_id)
    if (
        current_voice.conversation_id is not None
        and payload.conversation_id is not None
        and current_voice.conversation_id != payload.conversation_id
    ):
        raise ConflictError(
            "VOICE_SESSION_MISMATCH",
            "conversation_id does not match the active voice session",
        )
    result = await services.sessions.set_voice_status(
        session_id,
        token=token,
        event_id=payload.event_id,
        status=voice_status,
    )
    try:
        await services.voice_sessions.update(
            session_id,
            voice_status,
            conversation_id=payload.conversation_id,
            now=datetime.now(UTC),
        )
    except VoiceConversationError as exc:
        raise ConflictError("VOICE_SESSION_MISMATCH", str(exc)) from exc
    return CommandResponse(
        duplicate=result.duplicate,
        session=_session_view(result.session, services),
    )


async def _authenticate_websocket(
    websocket: WebSocket,
    services: AppServices,
) -> str | None:
    expected_dashboard_key = services.settings.dashboard_api_key
    if expected_dashboard_key is None and services.settings.environment in {
        "development",
        "test",
    }:
        return None
    try:
        payload = await asyncio.wait_for(
            websocket.receive_json(),
            timeout=services.settings.websocket_auth_timeout_seconds,
        )
    except (TimeoutError, ValueError, RuntimeError, WebSocketDisconnect):
        await websocket.close(code=1008, reason="Authentication required")
        raise AuthenticationError("WebSocket authentication failed") from None
    if not isinstance(payload, dict) or payload.get("type") != "auth":
        await websocket.close(code=1008, reason="Authentication required")
        raise AuthenticationError("WebSocket authentication failed")
    dashboard_key = payload.get("dashboard_key")
    if (
        expected_dashboard_key is not None
        and isinstance(dashboard_key, str)
        and hmac.compare_digest(expected_dashboard_key, dashboard_key)
    ):
        return None
    session_id = payload.get("session_id")
    control_token = payload.get("control_token")
    if isinstance(session_id, str) and isinstance(control_token, str):
        try:
            await services.sessions.authorize(session_id, control_token)
        except AuthenticationError:
            await websocket.close(code=1008, reason="Authentication failed")
            raise
        return session_id
    await websocket.close(code=1008, reason="Authentication failed")
    raise AuthenticationError("WebSocket authentication failed")


@router.websocket("/ws/events")
async def websocket_events(websocket: WebSocket) -> None:
    services: AppServices = websocket.app.state.services
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in services.settings.cors_origins:
        await websocket.close(code=1008, reason="Origin is not allowed")
        return
    await websocket.accept()
    try:
        session_scope = await _authenticate_websocket(websocket, services)
    except AuthenticationError:
        return
    try:
        async with services.broker.subscribe() as queue:
            snapshot = await _status(services, session_id=session_scope)
            snapshot_sequence = snapshot.latest_sequence
            await websocket.send_json(
                {
                    "event_id": f"snapshot-{uuid4()}",
                    "sequence": snapshot_sequence,
                    "type": "system.snapshot",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "session_id": session_scope,
                    "device_id": None,
                    "data": snapshot.model_dump(mode="json"),
                }
            )
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=services.settings.websocket_heartbeat_seconds,
                    )
                    if event.sequence <= snapshot_sequence:
                        continue
                    if session_scope is not None and event.session_id != session_scope:
                        continue
                    await websocket.send_json(event.model_dump(mode="json"))
                except TimeoutError:
                    await websocket.send_json(
                        {
                            "event_id": f"heartbeat-{uuid4()}",
                            "sequence": 0,
                            "type": "system.heartbeat",
                            "occurred_at": datetime.now(UTC).isoformat(),
                            "session_id": session_scope,
                            "device_id": None,
                            "data": {},
                        }
                    )
    except (WebSocketDisconnect, RuntimeError):
        return
