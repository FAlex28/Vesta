from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceEventRequest(StrictModel):
    event_id: Identifier
    device_id: Identifier
    event: Literal["activate"]
    occurred_at: datetime | None = None


class SessionView(StrictModel):
    id: str
    device_id: str
    state: str
    created_at: datetime
    updated_at: datetime
    checkin_attempt: int
    missed_attempts: int
    next_checkin_at: datetime | None
    response_deadline: datetime | None
    retry_at: datetime | None
    client_last_seen_at: datetime | None
    connection_lease_expires_at: datetime | None
    active_technical_issues: list[str]
    alert_reason: str | None


class ActivationResponse(StrictModel):
    duplicate: bool
    session: SessionView
    control_token: str
    control_token_expires_at: datetime


class CheckInRequest(StrictModel):
    event_id: Identifier
    confirmation: Literal["safe"] = "safe"
    source: Literal["android", "voice_agent", "dashboard"] = "android"


class SosRequest(StrictModel):
    event_id: Identifier
    source: Literal["android", "voice_agent", "dashboard"] = "android"
    reason: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=280)] = None


class CancelRequest(StrictModel):
    event_id: Identifier
    reason: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=280)] = None


class TechnicalStatusRequest(StrictModel):
    event_id: Identifier
    issue: Literal[
        "network_disconnected",
        "voice_unavailable",
        "microphone_denied",
        "audio_interrupted",
    ]
    active: bool
    source: Literal["android", "voice_gateway", "backend"] = "android"


class HeartbeatRequest(StrictModel):
    pass


class VoiceAccessRequest(StrictModel):
    transport: Literal["webrtc"] = "webrtc"
    participant_name: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
    ] = None
    environment: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
    ] = None


class VoiceAccessResponse(StrictModel):
    provider: Literal["elevenlabs", "demo"]
    mode: Literal["live", "demo"]
    transport: Literal["webrtc", "scripted"]
    conversation_token: str | None = None
    conversation_id: str | None = None
    client_config: dict[str, Any]


class CommandResponse(StrictModel):
    duplicate: bool
    session: SessionView


class SystemStatusResponse(StrictModel):
    system_state: str
    generated_at: datetime
    latest_sequence: int
    sessions: list[SessionView]
    voice_mode: Literal["elevenlabs", "demo"]


class ErrorBody(StrictModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(StrictModel):
    error: ErrorBody
