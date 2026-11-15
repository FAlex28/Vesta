from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import StreamingResponse

from app.assistant import AssistantAction, AssistantUnavailable
from app.container import AppServices
from app.errors import (
    AppError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.safety import (
    AudioStatus,
    CancellationReason,
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
    AccessGrantResponse,
    ActivationResponse,
    AlertLocationView,
    AlertView,
    ArchivedRecordingView,
    AssistantPromptRequest,
    AssistantReplyRequest,
    AssistantReplyResponse,
    AssistantSpeechView,
    CallAnswerRequest,
    CallOfferRequest,
    CallView,
    CancelRequest,
    CheckInDeliveryRequest,
    CheckInRequest,
    ClientStatusRequest,
    CommandResponse,
    CompanionProfileResponse,
    CompanionSessionResponse,
    ContactAlertView,
    ContactClaimRequest,
    ContactCreateRequest,
    ContactInviteView,
    ContactLocationView,
    ContactStatusResponse,
    ConversationResponse,
    ConversationTurnView,
    DeviceEventRequest,
    DeviceLocationRequest,
    DeviceLocationResponse,
    DevicePairRequest,
    EmergencyContactView,
    HeartbeatRequest,
    LocationHistoryResponse,
    LocationPointView,
    LocationUpdateRequest,
    LocationUpdateResponse,
    LocationView,
    OpenAccessRequest,
    OperatorMessageAck,
    OperatorMessageView,
    PhoneView,
    PowerView,
    ProfileUpdateRequest,
    PushKeyResponse,
    PushSubscribeRequest,
    PushUnsubscribeRequest,
    RecordingChunkResponse,
    RecordingView,
    ResolveAlertRequest,
    SessionView,
    SimulatorPressRequest,
    SimulatorPressResponse,
    SosRequest,
    SystemStatusResponse,
    TakeoverResponse,
    TechnicalStatusRequest,
    VoiceAccessRequest,
    VoiceAccessResponse,
    VoiceStatusRequest,
)
from app.services.access import AccessGrant, AccessScope, codes_match
from app.services.alerts import AlertRecord, AlertStatus
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
        viewed_at=alert.viewed_at,
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
        operator_connected_at=services.takeovers.get(session.session_id),
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
    now = datetime.now(UTC)
    alert_locations: list[AlertLocationView] = []
    for session in sessions:
        alert = services.alerts.get_for_session(session.session_id)
        if alert is None or alert.status is not AlertStatus.ACTIVE:
            continue
        location = await services.locations.get_last_authorized(session.session_id)
        if location is not None:
            alert_locations.append(
                AlertLocationView(
                    session_id=session.session_id,
                    **_location_fix(location, now, services.settings.gps_stale_seconds),
                )
            )
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
        power=PowerView(**services.power) if services.power is not None else None,
        alert_locations=alert_locations,
        assistant=services.assistant.capabilities if services.assistant is not None else {},
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


def _camera_auth_headers(services: AppServices) -> dict[str, str]:
    key = services.settings.camera_api_key
    return {"X-Vesta-Camera-Key": key} if key else {}


def _camera_disconnected() -> dict[str, Any]:
    camera = {"connected": False, "device": None, "mode": None, "fps": 0, "error": None}
    return {"configured": False, "reachable": False, "thermal": camera, "rgb": camera.copy()}


@router.get("/api/v1/cameras/status", tags=["cameras"])
async def camera_status(
    request: Request,
    x_dashboard_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    services = _services(request)
    _require_dashboard_key(services, x_dashboard_key)
    base_url = services.settings.camera_base_url
    if not base_url:
        return _camera_disconnected()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=1.5)) as client:
            response = await client.get(
                f"{base_url}/api/status", headers=_camera_auth_headers(services)
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        unavailable = _camera_disconnected()
        unavailable["configured"] = True
        return unavailable

    def camera_view(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {"connected": False, "device": None, "mode": None, "fps": 0}
        fps = value.get("fps")
        return {
            "connected": bool(value.get("connected")),
            "device": value.get("device") if isinstance(value.get("device"), str) else None,
            "mode": value.get("mode") if isinstance(value.get("mode"), str) else None,
            "fps": fps if isinstance(fps, (int, float)) else 0,
        }

    return {
        "configured": True,
        "reachable": True,
        "thermal": camera_view(payload.get("thermal")),
        "rgb": camera_view(payload.get("rgb")),
    }


@router.get("/api/v1/cameras/{camera}/stream", tags=["cameras"])
async def camera_stream(
    camera: str,
    request: Request,
    x_dashboard_key: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    services = _services(request)
    _require_dashboard_key(services, x_dashboard_key)
    if camera not in {"thermal", "rgb"}:
        raise HTTPException(status_code=404, detail="Fluxul camerei nu există.")
    base_url = services.settings.camera_base_url
    if not base_url:
        raise HTTPException(status_code=503, detail="Serverul camerelor nu este configurat.")

    client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
    upstream_url = f"{base_url}/stream/{camera}.mjpg"
    try:
        upstream = await client.send(
            client.build_request("GET", upstream_url, headers=_camera_auth_headers(services)),
            stream=True,
        )
        upstream.raise_for_status()
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=503, detail="Fluxul camerei nu este disponibil.") from exc

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                if await request.is_disconnected():
                    break
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        media_type=upstream.headers.get("content-type", "multipart/x-mixed-replace"),
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _require_device_key(services: AppServices, supplied: str | None) -> None:
    expected_key = services.settings.device_api_key
    if expected_key is not None and (
        supplied is None or not hmac.compare_digest(expected_key, supplied)
    ):
        raise AuthenticationError("Invalid or missing device key")


async def _record_location(
    services: AppServices,
    session_id: str,
    *,
    event_id: str,
    latitude: float,
    longitude: float,
    captured_at: datetime,
    accuracy_meters: float | None,
    source: str,
) -> Any:
    try:
        result = await services.locations.record(
            event_id=event_id,
            session_id=session_id,
            latitude=latitude,
            longitude=longitude,
            captured_at=captured_at,
            received_at=datetime.now(UTC),
            accuracy_meters=accuracy_meters,
            source=source,
        )
    except LocationEventReused as exc:
        raise ConflictError("EVENT_ID_REUSED", str(exc)) from exc
    except LocationFromFuture as exc:
        raise ConflictError("LOCATION_TIMESTAMP_INVALID", str(exc)) from exc
    location = result.location
    if result.accepted_as_latest and not result.duplicate:
        if services.recordings is not None:
            services.recordings.add_location(
                session_id,
                latitude=location.latitude,
                longitude=location.longitude,
                captured_at=location.captured_at,
                accuracy_meters=location.accuracy_meters,
                source=source,
            )
        if services.persistence is not None:
            services.persistence.record_location(
                session_id=session_id,
                latitude=location.latitude,
                longitude=location.longitude,
                captured_at=location.captured_at,
                accuracy_meters=location.accuracy_meters,
                source=source,
            )
        alert = services.alerts.get_for_session(session_id)
        if alert and alert.status is AlertStatus.ACTIVE:
            await services.broker.publish(
                "location.updated",
                session_id=session_id,
                data={"alert_id": alert.alert_id, "captured_at": location.captured_at.isoformat()},
            )
    return result


DEVICE_FIX_MAX_AGE = timedelta(seconds=60)


async def _attach_device_fix(services: AppServices, session_id: str, event_id: str) -> None:
    fix = services.device_fix
    if not fix or datetime.now(UTC) - fix["captured_at"] > DEVICE_FIX_MAX_AGE:
        return
    try:
        await _record_location(
            services,
            session_id,
            event_id=f"{event_id}-device-fix"[:128],
            latitude=fix["latitude"],
            longitude=fix["longitude"],
            captured_at=fix["captured_at"],
            accuracy_meters=fix["accuracy_meters"],
            source="device_gps",
        )
    except ConflictError:
        return


@router.post("/api/v1/device/location", response_model=DeviceLocationResponse, tags=["device"])
async def device_location(
    payload: DeviceLocationRequest,
    request: Request,
    response: Response,
    x_device_key: Annotated[str | None, Header()] = None,
) -> DeviceLocationResponse:
    """The GPS on the Vesta device (Raspberry Pi UART) reports its fix every few seconds."""

    services = _services(request)
    _require_device_key(services, x_device_key)
    response.headers["Cache-Control"] = "no-store"
    if payload.device_id != services.settings.gpio_device_id:
        raise NotFoundError("Unknown device")
    services.device_fix = {
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "captured_at": payload.captured_at,
        "accuracy_meters": payload.accuracy_meters,
        "satellites": payload.satellites,
        "received_at": datetime.now(UTC),
    }
    session = await services.sessions.current_for_device(payload.device_id)
    if session is None:
        return DeviceLocationResponse(session_id=None, accepted_as_latest=False)
    result = await _record_location(
        services,
        session.session_id,
        event_id=payload.event_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        captured_at=payload.captured_at,
        accuracy_meters=payload.accuracy_meters,
        source="device_gps",
    )
    return DeviceLocationResponse(
        session_id=session.session_id, accepted_as_latest=result.accepted_as_latest
    )


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
    _require_device_key(services, x_device_key)
    result = await services.sessions.activate(
        event_id=payload.event_id,
        device_id=payload.device_id,
        now=payload.occurred_at,
    )
    if result.outcome is ActivationOutcome.ACTIVATED:
        # Data flows from the first second: the device's latest GPS fix joins the session.
        await _attach_device_fix(services, result.session.session_id, payload.event_id)
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
    if services.persistence is not None:
        services.persistence.record_companion_heartbeat(
            device_id=result.session.device_id,
            session_id=session_id,
            visibility=result.session.page_visibility.value,
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
    response.headers["Cache-Control"] = "no-store"
    result = await _record_location(
        services,
        session_id,
        event_id=payload.event_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        captured_at=payload.captured_at,
        accuracy_meters=payload.accuracy_meters,
        source=payload.source,
    )
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


# ---------------------------------------------------------------------------
# The person's phone (paired with the code printed on the device) and her
# emergency contacts (invited from her phone). The dispatcher hands out nothing.
# ---------------------------------------------------------------------------


def _companion(services: AppServices, authorization: str | None) -> tuple[AccessGrant, dict]:
    grant = services.access_tokens.verify(_bearer(authorization), AccessScope.COMPANION)
    phone = services.directory.phone_for_grant(grant.token_id)
    if phone is None:
        raise AuthenticationError("This phone is no longer paired")
    return grant, phone


def _contact(services: AppServices, authorization: str | None) -> tuple[AccessGrant, dict]:
    grant = services.access_tokens.verify(_bearer(authorization), AccessScope.CONTACT)
    contact = services.directory.contact_for_grant(grant.token_id)
    if contact is None:
        raise AuthenticationError("This emergency contact was removed")
    return grant, contact


def _location_fix(location: Any, now: datetime, stale_after: float) -> dict[str, Any]:
    age = max(0.0, (now - location.captured_at).total_seconds())
    return {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "captured_at": location.captured_at,
        "age_seconds": round(age, 1),
        "is_stale": age > stale_after,
        "accuracy_meters": location.accuracy_meters,
        "source": location.source,
    }


@router.post("/api/v1/pairing/device", response_model=AccessGrantResponse, tags=["access"])
async def pair_with_device_code(
    payload: DevicePairRequest,
    request: Request,
    response: Response,
) -> AccessGrantResponse:
    """The phone scans/types the code printed on the Vesta device, once."""

    services = _services(request)
    services.attempts.check()
    expected = services.directory.device_code(services.settings.device_pairing_code)
    if not codes_match(payload.code, expected):
        services.attempts.failed()
        raise AuthenticationError("Device code is not valid")
    token, expires_at, token_id = services.access_tokens.issue(
        AccessScope.COMPANION,
        services.settings.gpio_device_id,
        ttl=timedelta(seconds=services.settings.companion_token_ttl_seconds),
    )
    services.directory.register_phone(token_id, payload.label)
    response.headers["Cache-Control"] = "no-store"
    return AccessGrantResponse(
        scope="companion",
        device_id=services.settings.gpio_device_id,
        access_token=token,
        expires_at=expires_at,
    )


@router.get("/api/v1/companion/session", response_model=CompanionSessionResponse, tags=["access"])
async def companion_session(
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
) -> CompanionSessionResponse:
    """The paired phone asks for its device's running session and a control token."""

    services = _services(request)
    grant, _phone = _companion(services, authorization)
    response.headers["Cache-Control"] = "no-store"
    claimed = await services.sessions.claim_current_session(grant.device_id)
    if claimed is None:
        return CompanionSessionResponse(
            device_id=grant.device_id, session=None, control_token=None, control_token_expires_at=None
        )
    session, token, expires_at = claimed
    return CompanionSessionResponse(
        device_id=grant.device_id,
        session=_session_view(session, services),
        control_token=token,
        control_token_expires_at=expires_at,
    )


def _profile(services: AppServices, grant: AccessGrant, phone: dict) -> CompanionProfileResponse:
    directory = services.directory
    return CompanionProfileResponse(
        device_id=grant.device_id,
        display_name=directory.owner_name(),
        this_phone_id=phone["id"],
        phones=[PhoneView(**{k: p[k] for k in ("id", "label", "created_at", "last_seen_at")}) for p in directory.phones()],
        contacts=[EmergencyContactView(**c) for c in directory.contacts()],
    )


@router.get("/api/v1/companion/profile", response_model=CompanionProfileResponse, tags=["access"])
async def companion_profile(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CompanionProfileResponse:
    services = _services(request)
    grant, phone = _companion(services, authorization)
    return _profile(services, grant, phone)


@router.post("/api/v1/companion/profile", response_model=CompanionProfileResponse, tags=["access"])
async def update_companion_profile(
    payload: ProfileUpdateRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CompanionProfileResponse:
    """The name the emergency contacts see in invitations and alerts."""

    services = _services(request)
    grant, phone = _companion(services, authorization)
    services.directory.set_owner_name(payload.display_name)
    return _profile(services, grant, phone)


@router.post(
    "/api/v1/companion/phones/{phone_id}/revoke",
    response_model=CompanionProfileResponse,
    tags=["access"],
)
async def revoke_phone(
    phone_id: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CompanionProfileResponse:
    services = _services(request)
    grant, phone = _companion(services, authorization)
    services.directory.revoke_phone(phone_id)
    return _profile(services, grant, phone)


@router.post("/api/v1/companion/contacts", response_model=ContactInviteView, tags=["access"])
async def add_emergency_contact(
    payload: ContactCreateRequest,
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
) -> ContactInviteView:
    """Add an emergency contact and create its one-time invite code (sent by the phone)."""

    services = _services(request)
    _companion(services, authorization)
    contact = services.directory.add_contact(payload.name, payload.phone)
    code, expires_at = services.directory.create_invite(
        contact["id"], ttl=timedelta(seconds=services.settings.contact_invite_ttl_seconds)
    )
    response.headers["Cache-Control"] = "no-store"
    return ContactInviteView(
        contact=EmergencyContactView(**services.directory.contact_view(contact["id"])),
        code=code,
        expires_at=expires_at,
    )


@router.post(
    "/api/v1/companion/contacts/{contact_id}/invite",
    response_model=ContactInviteView,
    tags=["access"],
)
async def reinvite_emergency_contact(
    contact_id: str,
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
) -> ContactInviteView:
    services = _services(request)
    _companion(services, authorization)
    code, expires_at = services.directory.create_invite(
        contact_id, ttl=timedelta(seconds=services.settings.contact_invite_ttl_seconds)
    )
    response.headers["Cache-Control"] = "no-store"
    return ContactInviteView(
        contact=EmergencyContactView(**services.directory.contact_view(contact_id)),
        code=code,
        expires_at=expires_at,
    )


@router.post(
    "/api/v1/companion/contacts/{contact_id}/revoke",
    response_model=CompanionProfileResponse,
    tags=["access"],
)
async def revoke_emergency_contact(
    contact_id: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CompanionProfileResponse:
    services = _services(request)
    grant, phone = _companion(services, authorization)
    services.directory.revoke_contact(contact_id)
    return _profile(services, grant, phone)


@router.post("/api/v1/contact/claim", response_model=AccessGrantResponse, tags=["access"])
async def claim_contact_invite(
    payload: ContactClaimRequest,
    request: Request,
    response: Response,
) -> AccessGrantResponse:
    """The emergency contact opens the SMS link (or types the code) once."""

    services = _services(request)
    services.attempts.check()
    token, expires_at, token_id = services.access_tokens.issue(
        AccessScope.CONTACT,
        services.settings.gpio_device_id,
        ttl=timedelta(seconds=services.settings.contact_token_ttl_seconds),
    )
    if services.directory.claim_invite(payload.code, token_id) is None:
        services.attempts.failed()
        raise AuthenticationError("Invitation code is not valid or has expired")
    response.headers["Cache-Control"] = "no-store"
    return AccessGrantResponse(
        scope="contact",
        device_id=services.settings.gpio_device_id,
        access_token=token,
        expires_at=expires_at,
    )


@router.get("/api/v1/contact/status", response_model=ContactStatusResponse, tags=["access"])
async def contact_status(
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
) -> ContactStatusResponse:
    """Alerts of the contact's device. Coordinates only while an alert is ACTIVE."""

    services = _services(request)
    grant, contact = _contact(services, authorization)
    now = datetime.now(UTC)
    alerts: list[ContactAlertView] = []
    for session in await services.sessions.list_sessions():
        if session.device_id != grant.device_id:
            continue
        alert = services.alerts.get_for_session(session.session_id)
        alert_view = _alert_view(alert)
        if alert is None or alert_view is None:
            continue
        location_view: ContactLocationView | None = None
        location_status = "UNAVAILABLE"
        if alert.status is AlertStatus.ACTIVE:
            location = await services.locations.get_last_authorized(session.session_id)
            if location is not None:
                fix = _location_fix(location, now, services.settings.gps_stale_seconds)
                location_view = ContactLocationView(**fix)
                location_status = "STALE" if fix["is_stale"] else "CURRENT"
        alerts.append(
            ContactAlertView(
                session_id=session.session_id,
                state=session.state.value,
                alert_reason=session.alert_reason.value if session.alert_reason else None,
                alert=alert_view,
                location=location_view,
                location_status=location_status,
                push_status=services.directory.push_summary(alert.alert_id).get("ALERT_RAISED"),
            )
        )
    alerts.sort(key=lambda item: (item.alert.status != "ACTIVE", -item.alert.created_at.timestamp()))
    response.headers["Cache-Control"] = "no-store"
    return ContactStatusResponse(
        device_id=grant.device_id,
        generated_at=now,
        person_name=services.directory.owner_name(),
        contact_name=contact["name"],
        push_enabled=contact["push_enabled"],
        alerts=alerts,
    )


@router.post("/api/v1/contact/alerts/{alert_id}/viewed", response_model=AlertView, tags=["access"])
async def contact_viewed_alert(
    alert_id: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AlertView:
    """The contact page displayed this alert. Distinct from "notification sent"."""

    services = _services(request)
    grant, _contact_info = _contact(services, authorization)
    record = await services.sessions.mark_alert_viewed(alert_id, device_id=grant.device_id)
    if services.persistence is not None:
        services.persistence.record_alert_viewed(alert_id)
    view = _alert_view(record)
    assert view is not None
    return view


@router.get("/api/v1/contact/push/key", response_model=PushKeyResponse, tags=["access"])
async def contact_push_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> PushKeyResponse:
    services = _services(request)
    _contact(services, authorization)
    public = services.settings.vapid_public_key or services.directory.vapid()["public_key"]
    return PushKeyResponse(public_key=public)


@router.post("/api/v1/contact/push/subscribe", status_code=204, tags=["access"])
async def contact_push_subscribe(
    payload: PushSubscribeRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    services = _services(request)
    _grant, contact = _contact(services, authorization)
    if not payload.endpoint.startswith("https://"):
        raise AuthenticationError("Push endpoint must use HTTPS")
    services.directory.add_subscription(
        contact["id"],
        {"endpoint": payload.endpoint, "keys": {"p256dh": payload.keys.p256dh, "auth": payload.keys.auth}},
    )
    return Response(status_code=204)


@router.post("/api/v1/contact/push/unsubscribe", status_code=204, tags=["access"])
async def contact_push_unsubscribe(
    payload: PushUnsubscribeRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    services = _services(request)
    _contact(services, authorization)
    services.directory.remove_subscription(payload.endpoint)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Open access (VESTA_OPEN_ACCESS, development only): no pairing codes at all.
# ---------------------------------------------------------------------------


@router.post("/api/v1/access/open", response_model=AccessGrantResponse, tags=["access"])
async def open_access(
    payload: OpenAccessRequest,
    request: Request,
    response: Response,
) -> AccessGrantResponse:
    """Hackathon mode: the person's phone and the trusted contact open their pages directly."""

    services = _services(request)
    if not services.settings.open_access:
        raise NotFoundError("Open access is not enabled on this server")
    scope = AccessScope.COMPANION if payload.scope == "companion" else AccessScope.CONTACT
    ttl = (
        services.settings.companion_token_ttl_seconds
        if scope is AccessScope.COMPANION
        else services.settings.contact_token_ttl_seconds
    )
    token, expires_at, token_id = services.access_tokens.issue(
        scope, services.settings.gpio_device_id, ttl=timedelta(seconds=ttl)
    )
    if scope is AccessScope.COMPANION:
        services.directory.register_open_phone(token_id)
    else:
        services.directory.register_open_contact(token_id)
    response.headers["Cache-Control"] = "no-store"
    return AccessGrantResponse(
        scope=payload.scope,
        device_id=services.settings.gpio_device_id,
        access_token=token,
        expires_at=expires_at,
    )


@router.post("/api/v1/simulator/press", response_model=SimulatorPressResponse, tags=["access"])
async def simulator_press(payload: SimulatorPressRequest, request: Request) -> SimulatorPressResponse:
    """Demo only (VESTA_OPEN_ACCESS): the /buton page held for 5 s, instead of the joystick.

    Same activation as GPIO17 on the Pi, for the configured device; no device key leaves the server.
    """

    services = _services(request)
    if not services.settings.open_access:
        raise NotFoundError("The button simulator is not enabled on this server")
    result = await services.sessions.activate(
        event_id=payload.event_id, device_id=services.settings.gpio_device_id
    )
    if result.outcome is ActivationOutcome.ACTIVATED:
        await _attach_device_fix(services, result.session.session_id, payload.event_id)
    return SimulatorPressResponse(
        outcome=result.outcome.value,
        session_id=result.session.session_id,
        state=result.session.state.value,
    )


# ---------------------------------------------------------------------------
# Voice assistant in the earbuds, conversation recording.
# ---------------------------------------------------------------------------


def _assistant(services: AppServices):
    if services.assistant is None:
        raise ServiceUnavailableError("ASSISTANT_UNAVAILABLE", "The voice assistant is not configured")
    return services.assistant


def _speech_view(spoken: Any) -> AssistantSpeechView:
    return AssistantSpeechView(
        text=spoken.text,
        audio_base64=spoken.audio_base64(),
        mime_type=spoken.mime_type,
        turn_id=spoken.turn.id if spoken.turn else None,
    )


@router.post(
    "/api/v1/sessions/{session_id}/assistant/prompt",
    response_model=AssistantSpeechView,
    tags=["assistant"],
)
async def assistant_prompt(
    session_id: str,
    payload: AssistantPromptRequest,
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
) -> AssistantSpeechView:
    """What Vesta says now in the earbuds (text, plus ElevenLabs audio when configured)."""

    services = _services(request)
    session = await services.sessions.authorize(session_id, _bearer(authorization))
    line = payload.line
    if session.state is SessionState.CANCELLED and line != "false_alarm":
        raise ConflictError("SESSION_CANCELLED", "The session has ended")
    if line == "checkin":
        if session.current_attempt >= 2:
            line = "reprompt"
        elif session.last_confirmation_at is None:
            line = "greeting"
    if line == "nudge" and session.state not in {SessionState.ACTIVE, SessionState.ALERT}:
        raise ConflictError("NOT_NOW", "Periodic questions run only in ACTIVE or ALERT")
    if session_id in services.takeovers and line != "operator":
        raise ConflictError("OPERATOR_CONNECTED", "The dispatcher took over the call")
    spoken = await _assistant(services).say(
        session_id,
        line,
        event_id=payload.event_id,
        record=line != "false_alarm",
        state=session.state.value,
    )
    response.headers["Cache-Control"] = "no-store"
    return _speech_view(spoken)


@router.post(
    "/api/v1/sessions/{session_id}/assistant/reply",
    response_model=AssistantReplyResponse,
    tags=["assistant"],
)
async def assistant_reply(
    session_id: str,
    payload: AssistantReplyRequest,
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
) -> AssistantReplyResponse:
    """One spoken phrase of the person: Gemini interprets it, the API applies the intent."""

    services = _services(request)
    token = _bearer(authorization)
    session = await services.sessions.authorize(session_id, token)
    if session.state is SessionState.CANCELLED:
        raise ConflictError("SESSION_CANCELLED", "The session has ended")
    if session_id in services.takeovers:
        raise ConflictError("OPERATOR_CONNECTED", "The dispatcher took over the call")
    try:
        audio = base64.b64decode(payload.audio_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AppError(code="INVALID_AUDIO", message="audio_base64 is not valid base64", status_code=422) from exc
    alerting = session.state is SessionState.ALERT
    context = (
        f"Starea sesiunii: {session.state.value}. "
        + (
            "Dispeceratul și persoana de încredere au fost deja anunțați."
            if alerting
            else "Dispeceratul NU a fost anunțat încă; întrebarea este dacă a fost o greșeală."
        )
    )
    try:
        decision, spoken = await _assistant(services).understand(
            session_id,
            audio=audio,
            mime_type=payload.mime_type,
            context=context,
            event_id=payload.event_id,
        )
    except AssistantUnavailable as exc:
        raise ServiceUnavailableError("ASSISTANT_UNAVAILABLE", exc.message) from exc

    action_event = f"{payload.event_id}-act"[:128]
    applied = False
    try:
        if decision.action is AssistantAction.FALSE_ALARM and decision.transcript:
            await services.sessions.cancel(
                session_id,
                token=token,
                event_id=action_event,
                reason=CancellationReason.FALSE_ALARM.value,
            )
            applied = True
        elif decision.action is AssistantAction.ESCALATE and not alerting:
            reason = f"Cerere vocală: {decision.transcript}" if decision.transcript else "Cerere vocală"
            await services.sessions.request_sos(
                session_id,
                token=token,
                event_id=action_event,
                source="voice_agent",
                reason=reason[:280],
            )
            applied = True
        elif (
            decision.action is AssistantAction.CONFIRM_SAFE
            and session.state is SessionState.CHECK_IN
            and session.current_checkin_id
        ):
            await services.sessions.confirm_safe(
                session_id,
                token=token,
                event_id=action_event,
                checkin_id=session.current_checkin_id,
                source="voice_agent",
            )
            applied = True
    except ConflictError:
        applied = False  # the session moved on meanwhile (e.g. the check-in timed out)
    current = await services.sessions.authorize(session_id, token)
    response.headers["Cache-Control"] = "no-store"
    return AssistantReplyResponse(
        transcript=decision.transcript,
        action=decision.action.value,
        applied=applied,
        reply=_speech_view(spoken),
        session=_session_view(current, services),
    )


MAX_CHUNK_BYTES = 2 * 1024 * 1024


@router.post(
    "/api/v1/sessions/{session_id}/recording/chunks",
    response_model=RecordingChunkResponse,
    tags=["recording"],
)
async def upload_recording_chunk(
    session_id: str,
    request: Request,
    seq: Annotated[int, Query(ge=0, le=1_000_000)],
    authorization: Annotated[str | None, Header()] = None,
    content_type: Annotated[str | None, Header()] = None,
) -> RecordingChunkResponse:
    """The phone records the whole conversation from the start and uploads it in chunks."""

    services = _services(request)
    session = await services.sessions.authorize(session_id, _bearer(authorization))
    if session.cancellation_reason == CancellationReason.FALSE_ALARM.value:
        raise ConflictError("SESSION_CANCELLED", "False alarm: nothing is recorded any more")
    body = await request.body()
    if not body:
        raise AppError(code="EMPTY_CHUNK", message="The chunk is empty", status_code=422)
    if len(body) > MAX_CHUNK_BYTES:
        raise AppError(code="CHUNK_TOO_LARGE", message="A chunk may not exceed 2 MB", status_code=413)
    mime = (content_type or "audio/webm").split(";")[0].strip() or "audio/webm"
    if not mime.startswith("audio/"):
        raise AppError(code="INVALID_AUDIO", message="Only audio chunks are accepted", status_code=415)
    assert services.recordings is not None
    info = await services.recordings.append(session_id, seq, body, mime_type=mime)
    return RecordingChunkResponse(chunks=info.chunks, size_bytes=info.size_bytes)


async def _shared_with_dispatcher(services: AppServices, session_id: str, *, live: bool = True) -> None:
    """Share live sessions and real alerts with the authenticated dispatcher, never false alarms.

    `live=False`: only sessions that became an alert (the location route is private before that).
    """

    alert = services.alerts.get_for_session(session_id)
    if alert is not None:
        if alert.resolution_action == CancellationReason.FALSE_ALARM.value:
            raise NotFoundError("Nothing shared for this session")
        return
    try:
        session = await services.sessions.get(session_id)
    except NotFoundError:
        session = None
    if live and session and session.state in {
        SessionState.ACTIVE,
        SessionState.CHECK_IN,
        SessionState.UNCONFIRMED,
        SessionState.ALERT,
    }:
        return
    stored = services.recordings.record(session_id) if services.recordings else None
    if stored is not None and stored.alert_id is not None:
        if stored.resolution == CancellationReason.FALSE_ALARM.value:
            raise NotFoundError("Nothing shared for this session")
        return
    if services.persistence is not None:
        try:
            meta = await services.persistence.recording_meta_read(session_id)
        except Exception:
            meta = None
        if meta and meta.get("alert_id") and meta.get("resolution") != CancellationReason.FALSE_ALARM.value:
            return
    raise NotFoundError("Nothing shared for this session")


@router.get(
    "/api/v1/sessions/{session_id}/conversation",
    response_model=ConversationResponse,
    tags=["recording"],
)
async def session_conversation(
    session_id: str,
    request: Request,
    response: Response,
    x_dashboard_key: Annotated[str | None, Header()] = None,
) -> ConversationResponse:
    services = _services(request)
    _require_dashboard_key(services, x_dashboard_key)
    await _shared_with_dispatcher(services, session_id)
    info = services.recordings.info(session_id) if services.recordings else None
    turns = services.recordings.turns(session_id) if services.recordings else []
    views = [
        ConversationTurnView(id=t.id, role=t.role, text=t.text, at=t.at, action=t.action) for t in turns
    ]
    if not views and services.persistence is not None:
        try:
            rows = await services.persistence.conversation_turns(session_id)
        except Exception:
            rows = []
        views = [
            ConversationTurnView(
                id=str(row["turn_id"]),
                role=str(row["role"]).lower(),
                text=row["text"],
                at=row["occurred_at"],
                action=row.get("action"),
            )
            for row in rows
            if str(row["role"]).lower() in {"person", "assistant", "operator"}
        ]
    response.headers["Cache-Control"] = "no-store"
    return ConversationResponse(
        session_id=session_id,
        turns=views,
        recording=(
            RecordingView(
                mime_type=info.mime_type,
                size_bytes=info.size_bytes,
                chunks=info.chunks,
                started_at=info.started_at,
                updated_at=info.updated_at,
                finalized=info.finalized,
            )
            if info
            else None
        ),
    )


@router.get("/api/v1/sessions/{session_id}/recording", tags=["recording"])
async def session_recording(
    session_id: str,
    request: Request,
    x_dashboard_key: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """The recording from its first second, then live while the session runs."""

    services = _services(request)
    _require_dashboard_key(services, x_dashboard_key)
    await _shared_with_dispatcher(services, session_id)
    assert services.recordings is not None
    info = services.recordings.info(session_id)
    if info is None and services.persistence is not None:
        try:
            audio = await services.persistence.recording_audio(session_id)
        except Exception:
            audio = b""
        if audio:
            return Response(content=audio, media_type="audio/webm", headers={"Cache-Control": "no-store"})
    if info is None:
        raise NotFoundError("No recording for this session yet")

    async def relay():
        async for chunk in services.recordings.follow(session_id):
            if await request.is_disconnected():
                break
            yield chunk

    return StreamingResponse(
        relay(),
        media_type=info.mime_type,
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _archived(info) -> ArchivedRecordingView:
    return ArchivedRecordingView(
        session_id=info.session_id,
        device_id=info.device_id,
        started_at=info.started_at,
        updated_at=info.updated_at,
        size_bytes=info.size_bytes,
        chunks=info.chunks,
        finalized=info.finalized,
        alert_id=info.alert_id,
        alert_reason=info.alert_reason,
        alert_at=info.alert_at,
        resolution=info.resolution,
        resolved_at=info.resolved_at,
        operator_at=info.operator_at,
        turns=0,
    )


@router.get("/api/v1/analytics", tags=["analytics"])
async def live_analytics(
    request: Request,
    response: Response,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
    x_dashboard_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Live metrics from Tiger Data: continuous aggregates, alert response times, compression.
    Without VESTA_PERSISTENCE=tiger there is no history, so the dashboard hides the panel."""

    services = _services(request)
    _require_dashboard_key(services, x_dashboard_key)
    response.headers["Cache-Control"] = "no-store"
    if services.persistence is None:
        return {"enabled": False}
    try:
        snapshot = await services.persistence.analytics(days=days, hours=hours)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Tiger Data unavailable") from exc
    return {"enabled": True, **snapshot}


@router.get("/api/v1/recordings", response_model=list[ArchivedRecordingView], tags=["recording"])
async def recordings_archive(
    request: Request,
    response: Response,
    x_dashboard_key: Annotated[str | None, Header()] = None,
) -> list[ArchivedRecordingView]:
    """Every alert's recording kept in the database, newest first (false alarms are deleted)."""

    services = _services(request)
    _require_dashboard_key(services, x_dashboard_key)
    assert services.recordings is not None
    response.headers["Cache-Control"] = "no-store"
    items = []
    seen: set[str] = set()
    for info in services.recordings.archive():
        view = _archived(info)
        view.turns = len(services.recordings.turns(info.session_id))
        items.append(view)
        seen.add(info.session_id)
    if services.persistence is not None:
        try:
            rows = await services.persistence.recordings_archive()
        except Exception:
            rows = []
        for row in rows:
            sid = str(row["session_id"])
            if sid in seen:
                continue
            items.append(
                ArchivedRecordingView(
                    session_id=sid,
                    device_id=row.get("device_id"),
                    started_at=row["started_at"],
                    updated_at=row["updated_at"],
                    size_bytes=row["size_bytes"],
                    chunks=row["chunks"],
                    finalized=bool(row["finalized"]),
                    alert_id=row.get("alert_id"),
                    alert_reason=row.get("alert_reason"),
                    alert_at=row.get("alert_at"),
                    resolution=row.get("resolution"),
                    resolved_at=row.get("resolved_at"),
                    operator_at=row.get("operator_at"),
                    turns=0,
                )
            )
        items.sort(key=lambda item: item.alert_at or item.started_at, reverse=True)
    return items


@router.post(
    "/api/v1/sessions/{session_id}/takeover", response_model=TakeoverResponse, tags=["recording"]
)
async def take_over_call(
    session_id: str,
    request: Request,
    x_dashboard_key: Annotated[str | None, Header()] = None,
) -> TakeoverResponse:
    """The dispatcher takes the call: the assistant in her earbuds stops and the dispatcher talks."""

    services = _services(request)
    _require_dashboard_key(services, x_dashboard_key)
    session = await services.sessions.get(session_id)
    if session.state not in {
        SessionState.ACTIVE,
        SessionState.CHECK_IN,
        SessionState.UNCONFIRMED,
        SessionState.ALERT,
    }:
        raise ConflictError("SESSION_NOT_ACTIVE", "Only an ongoing session can be taken over")
    at = services.takeovers.get(session_id)
    if at is None:
        at = services.takeovers[session_id] = datetime.now(UTC)
        assert services.recordings is not None
        await services.recordings.annotate(session_id, operator_at=at)
        await services.broker.publish(
            "call.taken_over", session_id=session_id, data={"operator_connected_at": at.isoformat()}
        )
    return TakeoverResponse(session_id=session_id, operator_connected_at=at)


@router.post(
    "/api/v1/sessions/{session_id}/operator/messages",
    response_model=OperatorMessageAck,
    tags=["recording"],
)
async def send_operator_message(
    session_id: str,
    request: Request,
    x_dashboard_key: Annotated[str | None, Header()] = None,
    content_type: Annotated[str | None, Header()] = None,
) -> OperatorMessageAck:
    """A recorded phrase of the dispatcher (push to talk), played in her earbuds."""

    services = _services(request)
    _require_dashboard_key(services, x_dashboard_key)
    if session_id not in services.takeovers:
        raise ConflictError("NOT_TAKEN_OVER", "Take over the call before talking")
    body = await request.body()
    if not body:
        raise AppError(code="EMPTY_AUDIO", message="The message is empty", status_code=422)
    if len(body) > MAX_CHUNK_BYTES:
        raise AppError(code="AUDIO_TOO_LARGE", message="A message may not exceed 2 MB", status_code=413)
    mime = (content_type or "audio/webm").split(";")[0].strip() or "audio/webm"
    if not mime.startswith("audio/"):
        raise AppError(code="INVALID_AUDIO", message="Only audio is accepted", status_code=415)
    assert services.recordings is not None
    message = await services.recordings.add_operator_message(session_id, body, mime)
    await services.broker.publish(
        "operator.message", session_id=session_id, data={"message_id": message.id}
    )
    text = None
    if services.assistant is not None:
        text = await services.assistant.transcribe(body, mime)
    turn_text = text or "Mesaj vocal al dispecerului."
    if text:
        services.recordings.set_turn_text(message.id, text)
    if services.persistence is not None:
        from app.assistant import ConversationTurn

        services.persistence.record_conversation_turn(
            session_id, ConversationTurn(id=message.id, role="operator", text=turn_text, at=message.at)
        )
    await services.broker.publish(
        "conversation.updated", session_id=session_id, data={"turn_id": message.id, "role": "operator"}
    )
    return OperatorMessageAck(id=message.id, at=message.at)


@router.get(
    "/api/v1/sessions/{session_id}/operator/messages",
    response_model=list[OperatorMessageView],
    tags=["recording"],
)
async def receive_operator_messages(
    session_id: str,
    request: Request,
    response: Response,
    after: Annotated[str | None, Query(max_length=64)] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> list[OperatorMessageView]:
    """The person's phone fetches the dispatcher's messages to play them in her earbuds."""

    services = _services(request)
    await services.sessions.authorize(session_id, _bearer(authorization))
    assert services.recordings is not None
    response.headers["Cache-Control"] = "no-store"
    return [
        OperatorMessageView(
            id=item.id,
            at=item.at,
            mime_type=item.mime_type,
            audio_base64=base64.b64encode(item.data).decode(),
        )
        for item in services.recordings.operator_messages(session_id, after=after)
    ]


# ---------------------------------------------------------------------------
# Live call: the dispatcher talks with her directly (WebRTC). The server only passes the
# offer/answer between the dashboard and her phone; audio goes peer to peer on the network.
# ---------------------------------------------------------------------------


async def _take_over(services: AppServices, session_id: str) -> None:
    if session_id in services.takeovers:
        return
    at = services.takeovers[session_id] = datetime.now(UTC)
    if services.recordings is not None:
        await services.recordings.annotate(session_id, operator_at=at)
    await services.broker.publish(
        "call.taken_over", session_id=session_id, data={"operator_connected_at": at.isoformat()}
    )


@router.get(
    "/api/v1/sessions/{session_id}/locations", response_model=LocationHistoryResponse, tags=["dispatcher"]
)
async def location_history(
    session_id: str,
    request: Request,
    response: Response,
    x_dashboard_key: Annotated[str | None, Header()] = None,
) -> LocationHistoryResponse:
    """Dispatcher: where she has been, for an alert (kept with the recording, never for a false alarm)."""

    services = _services(request)
    _require_dashboard_key(services, x_dashboard_key)
    await _shared_with_dispatcher(services, session_id, live=False)
    points = [
        LocationPointView(
            latitude=p.latitude,
            longitude=p.longitude,
            captured_at=p.captured_at,
            accuracy_meters=p.accuracy_meters,
            source=p.source,
        )
        for p in (services.recordings.locations(session_id) if services.recordings else [])
    ]
    if not points and services.persistence is not None:
        try:
            rows = await services.persistence.location_history(session_id)
        except Exception:
            rows = []
        points = [
            LocationPointView(
                latitude=row["lat"],
                longitude=row["lon"],
                captured_at=row["occurred_at"],
                accuracy_meters=row.get("accuracy_m"),
                source="device_gps" if str(row.get("source", "")).upper().startswith("DEVICE") else "android",
            )
            for row in rows
        ]
    response.headers["Cache-Control"] = "no-store"
    return LocationHistoryResponse(session_id=session_id, points=points)


@router.post("/api/v1/sessions/{session_id}/call/offer", response_model=CallView, tags=["call"])
async def call_offer(
    session_id: str,
    payload: CallOfferRequest,
    request: Request,
    x_dashboard_key: Annotated[str | None, Header()] = None,
) -> CallView:
    """The dispatcher calls her: the assistant stops, her phone answers by itself."""

    services = _services(request)
    _require_dashboard_key(services, x_dashboard_key)
    session = await services.sessions.get(session_id)
    if session.state is SessionState.CANCELLED:
        raise ConflictError("SESSION_CANCELLED", "The session has ended")
    await _take_over(services, session_id)
    call = services.calls[session_id] = {"id": str(uuid4()), "offer": payload.sdp, "answer": None}
    await services.broker.publish("call.offer", session_id=session_id, data={"call_id": call["id"]})
    return CallView(call_id=call["id"], offer=None, answer=None)


@router.get("/api/v1/sessions/{session_id}/call", response_model=CallView, tags=["call"])
async def call_for_phone(
    session_id: str,
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
) -> CallView:
    """Her phone asks whether the dispatcher is calling."""

    services = _services(request)
    await services.sessions.authorize(session_id, _bearer(authorization))
    response.headers["Cache-Control"] = "no-store"
    call = services.calls.get(session_id)
    if call is None:
        return CallView(call_id=None)
    return CallView(call_id=call["id"], offer=call["offer"] if call["answer"] is None else None)


@router.post("/api/v1/sessions/{session_id}/call/answer", response_model=CallView, tags=["call"])
async def call_answer(
    session_id: str,
    payload: CallAnswerRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CallView:
    services = _services(request)
    await services.sessions.authorize(session_id, _bearer(authorization))
    call = services.calls.get(session_id)
    if call is None or call["id"] != payload.call_id:
        raise ConflictError("CALL_ENDED", "This call is no longer open")
    call["answer"] = payload.sdp
    await services.broker.publish("call.answered", session_id=session_id, data={"call_id": call["id"]})
    return CallView(call_id=call["id"])


@router.get("/api/v1/sessions/{session_id}/call/answer", response_model=CallView, tags=["call"])
async def call_answer_for_dispatcher(
    session_id: str,
    request: Request,
    response: Response,
    x_dashboard_key: Annotated[str | None, Header()] = None,
) -> CallView:
    services = _services(request)
    _require_dashboard_key(services, x_dashboard_key)
    response.headers["Cache-Control"] = "no-store"
    call = services.calls.get(session_id)
    if call is None:
        return CallView(call_id=None)
    return CallView(call_id=call["id"], answer=call["answer"])


@router.post("/api/v1/sessions/{session_id}/call/hangup", response_model=CallView, tags=["call"])
async def call_hangup(
    session_id: str,
    request: Request,
    x_dashboard_key: Annotated[str | None, Header()] = None,
) -> CallView:
    services = _services(request)
    _require_dashboard_key(services, x_dashboard_key)
    if services.calls.pop(session_id, None) is not None:
        await services.broker.publish("call.ended", session_id=session_id, data={})
    return CallView(call_id=None)


async def purge_session_media(services: AppServices, session_id: str) -> None:
    """False alarm: voice, transcript and positions of the session are deleted."""

    if services.recordings is not None:
        await services.recordings.delete(session_id)
    if services.assistant is not None:
        await services.assistant.forget(session_id)
    forget = getattr(services.locations, "forget", None)
    if forget is not None:
        await forget(session_id)
    if services.persistence is not None:
        services.persistence.purge_session_media(session_id)
    await services.broker.publish("session.data_deleted", session_id=session_id, data={})


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


def _same_origin(origin: str, host: str | None) -> bool:
    """A page served by this backend (VESTA_FRONTEND_DIST) talks to its own host."""

    if not host:
        return False
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host.strip().lower()


@router.websocket("/ws/events")
async def websocket_events(websocket: WebSocket) -> None:
    services: AppServices = websocket.app.state.services
    origin = websocket.headers.get("origin")
    if (
        origin is not None
        and origin not in services.settings.cors_origins
        and not _same_origin(origin, websocket.headers.get("host"))
    ):
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
            every = services.settings.websocket_heartbeat_seconds
            loop = asyncio.get_running_loop()
            last_sent = loop.time()
            while True:
                try:
                    # Events of other sessions are skipped, so the beat follows what this socket
                    # actually received, not the queue: a phone must not go silent while others talk.
                    wait = max(0.0, every - (loop.time() - last_sent))
                    event = await asyncio.wait_for(queue.get(), timeout=wait)
                    if event.sequence <= snapshot_sequence:
                        continue
                    if session_scope is not None and event.session_id != session_scope:
                        if loop.time() - last_sent >= every:
                            raise TimeoutError
                        continue
                    await websocket.send_json(event.model_dump(mode="json"))
                    last_sent = loop.time()
                except TimeoutError:
                    last_sent = loop.time()
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
