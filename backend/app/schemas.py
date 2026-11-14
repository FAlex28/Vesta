from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
AlertAction = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceEventRequest(StrictModel):
    """Authenticated integration/test fallback for the internal GPIO event."""

    event_id: Identifier
    device_id: Identifier
    event: Literal["activate"]
    occurred_at: AwareDatetime | None = None


class AlertView(StrictModel):
    id: str
    incident_type: str
    status: Literal["ACTIVE", "RESOLVED"]
    created_at: datetime
    updated_at: datetime
    notification_requested_at: datetime
    resolved_at: datetime | None
    resolution_action: str | None
    version: int


class SessionView(StrictModel):
    id: str
    device_id: str
    state: str
    health_status: Literal["HEALTHY", "DEGRADED", "DISCONNECTED"]
    version: int
    created_at: datetime
    updated_at: datetime
    quick_cancel_until: datetime | None
    cancellation_reason: str | None
    checkin_attempt: int
    missed_attempts: int
    undelivered_attempts: int
    checkin_id: str | None
    checkin_status: str | None
    checkin_requested_at: datetime | None
    checkin_delivered_at: datetime | None
    last_checkin_id: str | None
    last_checkin_status: str | None
    last_confirmation_at: datetime | None
    next_checkin_at: datetime | None
    response_deadline: datetime | None
    retry_at: datetime | None
    client_last_seen_at: datetime | None
    connection_lease_expires_at: datetime | None
    page_visibility: str
    connectivity_status: str
    audio_status: str
    gps_status: str
    power_status: str
    active_technical_issues: list[str]
    alert_reason: str | None
    alert: AlertView | None


class ActivationResponse(StrictModel):
    outcome: Literal["ACTIVATED", "DUPLICATE", "ALREADY_ACTIVE"]
    duplicate: bool
    session: SessionView
    control_token: str
    control_token_expires_at: datetime


class CheckInRequest(StrictModel):
    event_id: Identifier
    checkin_id: Identifier
    confirmation: Literal["safe"] = "safe"
    source: Literal["android", "voice_agent", "dashboard", "web"] = "android"


class CheckInDeliveryRequest(StrictModel):
    event_id: Identifier
    status: Literal["DELIVERED", "UNDELIVERED"]
    channel: Literal["voice", "web", "android"] = "voice"


class SosRequest(StrictModel):
    event_id: Identifier
    source: Literal["android", "voice_agent", "dashboard", "web"] = "android"
    reason: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, max_length=280),
    ] = None


class CancelRequest(StrictModel):
    event_id: Identifier
    reason: Literal[
        "ACCIDENTAL_ACTIVATION",
        "USER_CANCELLED",
        "SESSION_ENDED",
    ] = "USER_CANCELLED"


class ResolveAlertRequest(StrictModel):
    event_id: Identifier
    action: AlertAction


class LocationUpdateRequest(StrictModel):
    event_id: Identifier
    latitude: Annotated[float, Field(ge=-90, le=90)]
    longitude: Annotated[float, Field(ge=-180, le=180)]
    captured_at: AwareDatetime
    accuracy_meters: Annotated[float | None, Field(ge=0)] = None
    source: Literal["android", "integration"] = "android"


class LocationView(StrictModel):
    latitude: float
    longitude: float
    captured_at: datetime
    received_at: datetime
    accuracy_meters: float | None
    source: str


class LocationUpdateResponse(StrictModel):
    duplicate: bool
    accepted_as_latest: bool
    location: LocationView


class TechnicalStatusRequest(StrictModel):
    event_id: Identifier
    issue: Literal[
        "network_disconnected",
        "voice_unavailable",
        "voice_timeout",
        "microphone_denied",
        "audio_interrupted",
        "audio_disconnected",
        "gps_stale",
        "gps_unavailable",
        "page_hidden",
        "low_voltage",
        "power_unstable",
        "process_restarted",
    ]
    active: bool
    source: Literal["android", "voice_gateway", "backend", "integration"] = "android"


class HeartbeatRequest(StrictModel):
    page_visibility: Literal["VISIBLE", "HIDDEN", "UNKNOWN"] | None = None


class ClientStatusRequest(StrictModel):
    event_id: Identifier
    page_visibility: Literal["VISIBLE", "HIDDEN", "UNKNOWN"] | None = None
    connectivity_status: Literal["CONNECTED", "RECONNECTING", "DISCONNECTED", "UNKNOWN"] | None = (
        None
    )
    audio_status: Literal["CONNECTED", "DISCONNECTED", "ERROR", "UNKNOWN"] | None = None
    gps_status: Literal["GPS_AVAILABLE", "GPS_STALE", "GPS_UNAVAILABLE"] | None = None
    power_status: (
        Literal["POWER_OK", "POWER_UNSTABLE", "LOW_VOLTAGE", "POWER_STATUS_UNKNOWN"] | None
    ) = None

    @model_validator(mode="after")
    def require_a_status(self) -> ClientStatusRequest:
        if all(
            value is None
            for value in (
                self.page_visibility,
                self.connectivity_status,
                self.audio_status,
                self.gps_status,
                self.power_status,
            )
        ):
            raise ValueError("at least one client status must be provided")
        return self


class VoiceStatusRequest(StrictModel):
    event_id: Identifier
    status: Literal[
        "CONNECTED",
        "DISCONNECTED",
        "API_ERROR",
        "MICROPHONE_ERROR",
        "TIMEOUT",
        "RECONNECTING",
        "RECONNECTED",
        "SERVICE_UNAVAILABLE",
    ]
    conversation_id: Identifier | None = None


class VoiceAccessRequest(StrictModel):
    transport: Literal["webrtc"] = "webrtc"
    participant_name: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
    ] = None
    environment: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
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
    system_health: Literal["HEALTHY", "DEGRADED", "DISCONNECTED"]
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
