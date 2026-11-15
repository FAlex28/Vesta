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
    viewed_at: datetime | None = None


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
    # The dispatcher took over the call: the assistant in the earbuds is silent from then on.
    operator_connected_at: datetime | None = None


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
        "FALSE_ALARM",
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


class PowerView(StrictModel):
    state: Literal["POWER_OK", "LOW_VOLTAGE", "POWER_UNSTABLE", "POWER_STATUS_UNKNOWN"]
    detail: str
    checked_at: datetime


class SystemStatusResponse(StrictModel):
    system_state: str
    system_health: Literal["HEALTHY", "DEGRADED", "DISCONNECTED"]
    generated_at: datetime
    latest_sequence: int
    sessions: list[SessionView]
    voice_mode: Literal["elevenlabs", "demo"]
    # Raspberry Pi power as read from vcgencmd; null until the first reading.
    power: PowerView | None = None
    # Dispatcher only: last authorized location of sessions whose alert is ACTIVE.
    alert_locations: list[AlertLocationView] = Field(default_factory=list)
    # Voice assistant: Gemini understands the person, ElevenLabs gives the voice.
    assistant: dict[str, bool] = Field(default_factory=dict)


Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=32)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]


class DevicePairRequest(StrictModel):
    """The code printed on the Vesta device (also inside its QR code)."""

    code: Code
    label: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] | None = None


class AccessGrantResponse(StrictModel):
    scope: Literal["companion", "contact"]
    device_id: str
    access_token: str
    expires_at: datetime


class PhoneView(StrictModel):
    id: str
    label: str
    created_at: datetime
    last_seen_at: datetime


class EmergencyContactView(StrictModel):
    id: str
    name: str
    phone: str
    created_at: datetime
    accepted_at: datetime | None
    push_enabled: bool
    invite_pending: bool


class CompanionProfileResponse(StrictModel):
    device_id: str
    display_name: str | None
    this_phone_id: str
    phones: list[PhoneView]
    contacts: list[EmergencyContactView]


class ProfileUpdateRequest(StrictModel):
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] | None = None


class ContactCreateRequest(StrictModel):
    name: ShortText
    phone: Annotated[str, StringConstraints(strip_whitespace=True, min_length=6, max_length=25)]


class ContactInviteView(StrictModel):
    contact: EmergencyContactView
    code: str
    expires_at: datetime


class ContactClaimRequest(StrictModel):
    code: Code


class PushKeys(StrictModel):
    p256dh: Annotated[str, StringConstraints(min_length=16, max_length=256)]
    auth: Annotated[str, StringConstraints(min_length=8, max_length=64)]


class PushSubscribeRequest(StrictModel):
    endpoint: Annotated[str, StringConstraints(min_length=10, max_length=1024)]
    keys: PushKeys
    expirationTime: float | None = None  # noqa: N815 - name defined by the Push API


class PushUnsubscribeRequest(StrictModel):
    endpoint: Annotated[str, StringConstraints(min_length=10, max_length=1024)]


class PushKeyResponse(StrictModel):
    public_key: str


class CompanionSessionResponse(StrictModel):
    device_id: str
    session: SessionView | None
    control_token: str | None
    control_token_expires_at: datetime | None


class LocationFix(StrictModel):
    latitude: float
    longitude: float
    captured_at: datetime
    age_seconds: float
    is_stale: bool
    accuracy_meters: float | None
    # "device_gps" (the GPS on the Vesta device) or "android" (the phone's browser).
    source: str | None = None


class ContactLocationView(LocationFix):
    pass


class AlertLocationView(LocationFix):
    """Last authorized location of a session whose alert is ACTIVE (dispatcher view)."""

    session_id: str


class ContactAlertView(StrictModel):
    session_id: str
    state: str
    alert_reason: str | None
    alert: AlertView
    # Only while the alert is ACTIVE; never after it is resolved.
    location: ContactLocationView | None
    location_status: Literal["CURRENT", "STALE", "UNAVAILABLE"]
    # Whether the push "alert raised" reached a push service for this contact's browsers.
    push_status: str | None = None


class ContactStatusResponse(StrictModel):
    device_id: str
    generated_at: datetime
    person_name: str | None
    contact_name: str
    push_enabled: bool
    alerts: list[ContactAlertView]


class ErrorBody(StrictModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(StrictModel):
    error: ErrorBody


class OpenAccessRequest(StrictModel):
    scope: Literal["companion", "contact"]


class SimulatorPressRequest(StrictModel):
    event_id: Identifier


class SimulatorPressResponse(StrictModel):
    outcome: Literal["ACTIVATED", "DUPLICATE", "ALREADY_ACTIVE"]
    session_id: str
    state: str


class DeviceLocationRequest(StrictModel):
    """A fix from the GPS module wired to the Vesta device (Raspberry Pi UART)."""

    event_id: Identifier
    device_id: Identifier
    latitude: Annotated[float, Field(ge=-90, le=90)]
    longitude: Annotated[float, Field(ge=-180, le=180)]
    captured_at: AwareDatetime
    accuracy_meters: Annotated[float | None, Field(ge=0)] = None
    satellites: Annotated[int | None, Field(ge=0, le=99)] = None


class DeviceLocationResponse(StrictModel):
    session_id: str | None
    accepted_as_latest: bool


class AssistantPromptRequest(StrictModel):
    event_id: Identifier
    line: Literal["checkin", "alert", "false_alarm", "confirm_safe", "nudge", "operator"] = "checkin"


class AssistantSpeechView(StrictModel):
    text: str
    audio_base64: str | None
    mime_type: str | None
    turn_id: str | None


class AssistantReplyRequest(StrictModel):
    event_id: Identifier
    # One spoken phrase as WAV (16 kHz mono), at most ~30 s.
    audio_base64: Annotated[str, StringConstraints(min_length=16, max_length=1_500_000)]
    mime_type: Literal["audio/wav", "audio/webm", "audio/ogg", "audio/mpeg"] = "audio/wav"


class AssistantReplyResponse(StrictModel):
    transcript: str
    action: Literal["NONE", "FALSE_ALARM", "ESCALATE", "CONFIRM_SAFE"]
    applied: bool
    reply: AssistantSpeechView
    session: SessionView


class ConversationTurnView(StrictModel):
    id: str
    role: Literal["person", "assistant", "operator"]
    text: str
    at: datetime
    action: str | None = None


class RecordingView(StrictModel):
    mime_type: str
    size_bytes: int
    chunks: int
    started_at: datetime
    updated_at: datetime
    finalized: bool


class ConversationResponse(StrictModel):
    session_id: str
    turns: list[ConversationTurnView]
    recording: RecordingView | None


class LocationPointView(StrictModel):
    latitude: float
    longitude: float
    captured_at: datetime
    accuracy_meters: float | None = None
    source: str


class LocationHistoryResponse(StrictModel):
    """The route of a session that became an alert (never a false alarm), oldest first."""

    session_id: str
    points: list[LocationPointView]


SdpText = Annotated[str, StringConstraints(min_length=10, max_length=20_000)]


class CallOfferRequest(StrictModel):
    sdp: SdpText


class CallAnswerRequest(StrictModel):
    call_id: Identifier
    sdp: SdpText


class CallView(StrictModel):
    """Live call between the dispatcher and her phone (WebRTC; the server only relays SDP)."""

    call_id: str | None
    offer: str | None = None
    answer: str | None = None


class TakeoverResponse(StrictModel):
    session_id: str
    operator_connected_at: datetime


class OperatorMessageView(StrictModel):
    id: str
    at: datetime
    mime_type: str
    audio_base64: str


class OperatorMessageAck(StrictModel):
    id: str
    at: datetime


class ArchivedRecordingView(StrictModel):
    session_id: str
    device_id: str | None
    started_at: datetime
    updated_at: datetime
    size_bytes: int
    chunks: int
    finalized: bool
    alert_id: str | None
    alert_reason: str | None
    alert_at: datetime | None
    resolution: str | None
    resolved_at: datetime | None
    operator_at: datetime | None
    turns: int


class RecordingChunkResponse(StrictModel):
    chunks: int
    size_bytes: int


SystemStatusResponse.model_rebuild()
