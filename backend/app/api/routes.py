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
from app.safety import SafetySession, SessionState, TechnicalIssue
from app.schemas import (
    ActivationResponse,
    CancelRequest,
    CheckInRequest,
    CommandResponse,
    DeviceEventRequest,
    HeartbeatRequest,
    SessionView,
    SosRequest,
    SystemStatusResponse,
    TechnicalStatusRequest,
    VoiceAccessRequest,
    VoiceAccessResponse,
)
from app.voice import VoiceProviderError

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


def _session_view(session: SafetySession) -> SessionView:
    if session.created_at is None or session.updated_at is None:
        raise RuntimeError("An activated session must have timestamps")
    return SessionView(
        id=session.session_id,
        device_id=session.device_id,
        state=session.state.value,
        created_at=session.created_at,
        updated_at=session.updated_at,
        checkin_attempt=session.current_attempt,
        missed_attempts=session.missed_attempts,
        next_checkin_at=session.next_check_in_at,
        response_deadline=session.response_deadline,
        retry_at=session.retry_at,
        client_last_seen_at=session.client_last_seen_at,
        connection_lease_expires_at=session.connection_lease_expires_at,
        active_technical_issues=sorted(issue.value for issue in session.technical_issues),
        alert_reason=session.alert_reason.value if session.alert_reason else None,
    )


async def _status(services: AppServices) -> SystemStatusResponse:
    snapshot = await services.sessions.snapshot()
    return SystemStatusResponse(
        system_state=snapshot.state.value,
        generated_at=datetime.now(UTC),
        latest_sequence=snapshot.latest_sequence,
        sessions=[_session_view(session) for session in snapshot.sessions],
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
async def system_status(request: Request) -> SystemStatusResponse:
    return await _status(_services(request))


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
    )
    response.status_code = status.HTTP_200_OK if result.duplicate else status.HTTP_201_CREATED
    response.headers["Cache-Control"] = "no-store"
    if result.control_token_expires_at is None:
        raise RuntimeError("Activation token is missing an expiry")
    return ActivationResponse(
        duplicate=result.duplicate,
        session=_session_view(result.session),
        control_token=result.control_token,
        control_token_expires_at=result.control_token_expires_at,
    )


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
    result = await _services(request).sessions.confirm_safe(
        session_id,
        token=_bearer(authorization),
        event_id=payload.event_id,
        source=payload.source,
    )
    return CommandResponse(duplicate=result.duplicate, session=_session_view(result.session))


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
    result = await _services(request).sessions.request_sos(
        session_id,
        token=_bearer(authorization),
        event_id=payload.event_id,
        source=payload.source,
        reason=payload.reason,
    )
    return CommandResponse(duplicate=result.duplicate, session=_session_view(result.session))


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
    result = await _services(request).sessions.cancel(
        session_id,
        token=_bearer(authorization),
        event_id=payload.event_id,
        reason=payload.reason,
    )
    return CommandResponse(duplicate=result.duplicate, session=_session_view(result.session))


@router.post(
    "/api/v1/sessions/{session_id}/heartbeat",
    response_model=CommandResponse,
    tags=["sessions"],
)
async def session_heartbeat(
    session_id: str,
    _payload: HeartbeatRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CommandResponse:
    result = await _services(request).sessions.heartbeat(
        session_id,
        token=_bearer(authorization),
    )
    return CommandResponse(duplicate=False, session=_session_view(result.session))


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
    result = await _services(request).sessions.set_technical_issue(
        session_id,
        token=_bearer(authorization),
        event_id=payload.event_id,
        issue=TechnicalIssue(payload.issue),
        active=payload.active,
        source=payload.source,
    )
    return CommandResponse(duplicate=result.duplicate, session=_session_view(result.session))


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
    session = await services.sessions.authorize(session_id, _bearer(authorization))
    if session.state is SessionState.CANCELLED:
        raise ConflictError("SESSION_CANCELLED", "A cancelled session cannot start voice")

    try:
        access = await services.voice.create_access(
            session_id=session_id,
            participant_name=payload.participant_name,
            environment=payload.environment,
        )
    except VoiceProviderError as exc:
        current = await services.sessions.get(session_id)
        if current.state is SessionState.CANCELLED:
            raise ConflictError(
                "SESSION_CANCELLED", "A cancelled session cannot start voice"
            ) from exc
        technical_result = await services.sessions.set_technical_issue(
            session_id,
            issue=TechnicalIssue.VOICE_UNAVAILABLE,
            active=True,
            event_id=f"internal-voice-error-{uuid4()}",
            source="voice_gateway",
            trusted_internal=True,
        )
        if technical_result.session.state is SessionState.CANCELLED:
            raise ConflictError(
                "SESSION_CANCELLED", "A cancelled session cannot start voice"
            ) from exc
        raise ServiceUnavailableError(f"VOICE_{exc.code.value.upper()}", str(exc)) from exc

    current = await services.sessions.get(session_id)
    if current.state is SessionState.CANCELLED:
        raise ConflictError("SESSION_CANCELLED", "A cancelled session cannot start voice")
    await services.sessions.set_technical_issue(
        session_id,
        issue=TechnicalIssue.VOICE_UNAVAILABLE,
        active=False,
        event_id=f"internal-voice-ready-{uuid4()}",
        source="voice_gateway",
        trusted_internal=True,
    )
    current = await services.sessions.get(session_id)
    if current.state is SessionState.CANCELLED:
        raise ConflictError("SESSION_CANCELLED", "A cancelled session cannot start voice")
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
    response.headers["Cache-Control"] = "no-store"
    return VoiceAccessResponse(
        provider=public["provider"],
        mode=public["mode"],
        transport=public["transport"],
        conversation_token=public.get("conversation_token"),
        conversation_id=public.get("conversation_id"),
        client_config=client_config,
    )


@router.websocket("/ws/events")
async def websocket_events(websocket: WebSocket) -> None:
    services: AppServices = websocket.app.state.services
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in services.settings.cors_origins:
        await websocket.close(code=1008, reason="Origin is not allowed")
        return
    await websocket.accept()
    try:
        async with services.broker.subscribe() as queue:
            snapshot = await _status(services)
            snapshot_sequence = snapshot.latest_sequence
            await websocket.send_json(
                {
                    "event_id": f"snapshot-{uuid4()}",
                    "sequence": snapshot_sequence,
                    "type": "system.snapshot",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "session_id": None,
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
                    await websocket.send_json(event.model_dump(mode="json"))
                except TimeoutError:
                    await websocket.send_json(
                        {
                            "event_id": f"heartbeat-{uuid4()}",
                            "sequence": 0,
                            "type": "system.heartbeat",
                            "occurred_at": datetime.now(UTC).isoformat(),
                            "session_id": None,
                            "device_id": None,
                            "data": {},
                        }
                    )
    except (WebSocketDisconnect, RuntimeError):
        return
