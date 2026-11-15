/* Types mirrored from shared/API_CONTRACT.md v5 and backend/app/schemas.py. */

export const SESSION_STATES = ["IDLE", "ACTIVE", "CHECK_IN", "UNCONFIRMED", "ALERT", "CANCELLED"] as const;
export type SessionState = (typeof SESSION_STATES)[number];

export type AlertReason = "MISSED_CHECK_INS" | "SOS_EXPLICIT" | string;

export type HealthStatus = "HEALTHY" | "DEGRADED" | "DISCONNECTED";
export type CheckInStatus = "REQUESTED" | "DELIVERED" | "UNDELIVERED" | "TIMED_OUT" | "CONFIRMED";

export type AlertView = {
  id: string;
  incident_type: string;
  status: "ACTIVE" | "RESOLVED";
  created_at: string;
  updated_at: string;
  notification_requested_at: string;
  resolved_at: string | null;
  resolution_action: string | null;
  version: number;
  /** First time the authorized trusted contact displayed the alert (not "notification sent"). */
  viewed_at?: string | null;
};

/** Technical issues accepted by POST /technical-status. */
export const CONTRACT_TECHNICAL_ISSUES = [
  "network_disconnected",
  "voice_unavailable",
  "microphone_denied",
  "audio_interrupted",
] as const;
export type ContractTechnicalIssue = (typeof CONTRACT_TECHNICAL_ISSUES)[number];

export type SessionView = {
  id: string;
  device_id: string;
  state: SessionState;
  health_status: HealthStatus;
  version: number;
  created_at: string;
  updated_at: string;
  quick_cancel_until: string | null;
  cancellation_reason: string | null;
  checkin_attempt: number;
  missed_attempts: number;
  undelivered_attempts: number;
  checkin_id: string | null;
  checkin_status: CheckInStatus | null;
  checkin_requested_at: string | null;
  checkin_delivered_at: string | null;
  last_checkin_id: string | null;
  last_checkin_status: CheckInStatus | null;
  last_confirmation_at: string | null;
  next_checkin_at: string | null;
  response_deadline: string | null;
  retry_at: string | null;
  client_last_seen_at: string | null;
  connection_lease_expires_at: string | null;
  page_visibility: "VISIBLE" | "HIDDEN" | "UNKNOWN";
  connectivity_status: "CONNECTED" | "RECONNECTING" | "DISCONNECTED" | "UNKNOWN";
  audio_status: "CONNECTED" | "DISCONNECTED" | "ERROR" | "UNKNOWN";
  gps_status: "GPS_AVAILABLE" | "GPS_STALE" | "GPS_UNAVAILABLE";
  power_status: "POWER_OK" | "POWER_UNSTABLE" | "LOW_VOLTAGE" | "POWER_STATUS_UNKNOWN";
  active_technical_issues: string[];
  alert_reason: string | null;
  alert: AlertView | null;
  /** The dispatcher took over the call: the assistant in the earbuds is silent. */
  operator_connected_at?: string | null;
};

export type SystemStatus = {
  system_state: SessionState;
  system_health: HealthStatus;
  generated_at: string;
  latest_sequence: number;
  sessions: SessionView[];
  voice_mode: "elevenlabs" | "demo";
  /** Raspberry Pi power from vcgencmd; null until the Pi has reported. */
  power?: { state: string; detail: string; checked_at: string } | null;
  /** Dispatcher only: last authorized location of sessions whose alert is ACTIVE. */
  alert_locations?: AlertLocation[];
  /** Voice assistant: Gemini understands the person, ElevenLabs gives the voice. */
  assistant?: { understanding?: boolean; voice?: boolean };
};

export type CameraFeedStatus = {
  connected: boolean;
  device?: string | null;
  mode?: string | null;
  fps?: number;
  error?: string | null;
};

export type CamerasStatus = {
  configured: boolean;
  reachable: boolean;
  thermal: CameraFeedStatus;
  rgb: CameraFeedStatus;
};

export type LocationFix = {
  latitude: number;
  longitude: number;
  captured_at: string;
  age_seconds: number;
  is_stale: boolean;
  accuracy_meters: number | null;
  /** "device_gps": the GPS on the Vesta device; "android": the phone's browser. */
  source?: string | null;
};

export type AlertLocation = LocationFix & { session_id: string };

export type AccessScope = "companion" | "contact";

export type AccessGrantResponse = { scope: AccessScope; device_id: string; access_token: string; expires_at: string };

export type PairedPhone = { id: string; label: string; created_at: string; last_seen_at: string };

export type EmergencyContact = {
  id: string;
  name: string;
  phone: string;
  created_at: string;
  accepted_at: string | null;
  push_enabled: boolean;
  invite_pending: boolean;
};

export type CompanionProfile = {
  device_id: string;
  display_name: string | null;
  this_phone_id: string;
  phones: PairedPhone[];
  contacts: EmergencyContact[];
};

export type ContactInvite = { contact: EmergencyContact; code: string; expires_at: string };

export type CompanionSessionResponse = {
  device_id: string;
  session: SessionView | null;
  control_token: string | null;
  control_token_expires_at: string | null;
};

export type ContactAlert = {
  session_id: string;
  state: SessionState;
  alert_reason: string | null;
  alert: AlertView;
  location: LocationFix | null;
  location_status: "CURRENT" | "STALE" | "UNAVAILABLE";
  push_status?: string | null;
};

export type ContactStatusResponse = {
  device_id: string;
  generated_at: string;
  person_name: string | null;
  contact_name: string;
  push_enabled: boolean;
  alerts: ContactAlert[];
};

export const EVENT_TYPES = [
  "session.activated",
  "session.already_active",
  "session.recovered",
  "safety.check_in_requested",
  "safety.check_in_delivered",
  "safety.check_in_undelivered",
  "safety.check_in_unconfirmed",
  "safety.check_in_confirmed",
  "safety.check_in_deferred",
  "safety.alerted",
  "alert.notification_requested",
  "safety.alert_resolved",
  "alert.resolved",
  "session.cancelled",
  "technical.issue_reported",
  "technical.issue_resolved",
  "technical.power",
  "contact.notification",
  "system.recovered",
  "client.status_changed",
  "voice.status_changed",
  "conversation.updated",
  "location.updated",
  "session.data_deleted",
  "call.taken_over",
  "operator.message",
  "system.snapshot",
  "system.heartbeat",
  "stream.resync_required",
] as const;

/** CancelRequest.reason in backend/app/schemas.py; any other value is rejected with 422. */
export type CancelReason = "ACCIDENTAL_ACTIVATION" | "USER_CANCELLED" | "SESSION_ENDED" | "FALSE_ALARM";

/** VoiceStatusRequest.status for POST /voice/status. */
export type VoiceStatus =
  | "CONNECTED"
  | "DISCONNECTED"
  | "API_ERROR"
  | "MICROPHONE_ERROR"
  | "TIMEOUT"
  | "RECONNECTING"
  | "RECONNECTED"
  | "SERVICE_UNAVAILABLE";

/** Resolution action sent by the phone when the user closes her own alert (contract example). */
export const USER_RESOLUTION_ACTION = "USER_CONFIRMED_SAFE";
export type EventType = (typeof EVENT_TYPES)[number];

export type EventEnvelope = {
  event_id: string;
  sequence: number;
  type: EventType | string;
  occurred_at: string;
  session_id: string | null;
  device_id: string | null;
  data: Record<string, unknown>;
};

export type CommandResponse = {
  duplicate: boolean;
  session: SessionView;
};

export type ClientStatusUpdate = Partial<{
  page_visibility: "VISIBLE" | "HIDDEN" | "UNKNOWN";
  connectivity_status: "CONNECTED" | "RECONNECTING" | "DISCONNECTED" | "UNKNOWN";
  audio_status: "CONNECTED" | "DISCONNECTED" | "ERROR" | "UNKNOWN";
  gps_status: "GPS_AVAILABLE" | "GPS_STALE" | "GPS_UNAVAILABLE";
  power_status: "POWER_OK" | "POWER_UNSTABLE" | "LOW_VOLTAGE" | "POWER_STATUS_UNKNOWN";
}>;

export type LocationUpdateResponse = {
  duplicate: boolean;
  accepted_as_latest: boolean;
  location: {
    latitude: number;
    longitude: number;
    captured_at: string;
    received_at: string;
    accuracy_meters: number | null;
    source: string;
  };
};

export type VoiceAccessResponse = {
  provider: "elevenlabs" | "demo";
  mode: "live" | "demo";
  transport: "webrtc" | "scripted";
  conversation_token: string | null;
  conversation_id: string | null;
  client_config: Record<string, unknown> & {
    demo_messages?: string[];
    session_id?: string;
  };
};

/** What Vesta says in the earbuds: text, plus ElevenLabs audio (mp3) when configured. */
export type AssistantSpeech = {
  text: string;
  audio_base64: string | null;
  mime_type: string | null;
  turn_id: string | null;
};

export type AssistantAction = "NONE" | "FALSE_ALARM" | "ESCALATE" | "CONFIRM_SAFE";

export type AssistantReply = {
  transcript: string;
  action: AssistantAction;
  applied: boolean;
  reply: AssistantSpeech;
  session: SessionView;
};

export type AssistantLine = "checkin" | "alert" | "false_alarm" | "confirm_safe" | "nudge" | "operator";

export type OperatorMessage = { id: string; at: string; mime_type: string; audio_base64: string };

export type ArchivedRecording = {
  session_id: string;
  device_id: string | null;
  started_at: string;
  updated_at: string;
  size_bytes: number;
  chunks: number;
  finalized: boolean;
  alert_id: string | null;
  alert_reason: string | null;
  alert_at: string | null;
  resolution: string | null;
  resolved_at: string | null;
  operator_at: string | null;
  turns: number;
};

export type ConversationTurn = {
  id: string;
  role: "person" | "assistant" | "operator";
  text: string;
  at: string;
  action: string | null;
};

export type RecordingInfo = {
  mime_type: string;
  size_bytes: number;
  chunks: number;
  started_at: string;
  updated_at: string;
  finalized: boolean;
};

export type ConversationResponse = {
  session_id: string;
  turns: ConversationTurn[];
  recording: RecordingInfo | null;
};

export const FALSE_ALARM = "FALSE_ALARM";

export type ApiErrorBody = {
  error: { code: string; message: string; details?: Record<string, unknown> };
};

/** States in which a session is still running (not finished). */
export const isUnfinished = (state: SessionState) => state !== "CANCELLED" && state !== "IDLE";

/** Capabilities that still require a cross-component contract decision. */
export const MISSING_CONTRACT = {
  pressProgress: "Evenimente de început/progres pentru apăsarea de 5 secunde",
  externalNotification: "Livrarea notificării în afara paginii contactului (SMS/push)",
} as const;
