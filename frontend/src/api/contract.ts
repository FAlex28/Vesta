/* Types mirrored from shared/API_CONTRACT.md v2 and backend/app/schemas.py. */

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
};

export type AccessScope = "companion" | "contact";

export type PairingCodeResponse = { scope: AccessScope; device_id: string; code: string; expires_at: string };

export type PairingClaimResponse = { scope: AccessScope; device_id: string; access_token: string; expires_at: string };

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
  location: {
    latitude: number;
    longitude: number;
    captured_at: string;
    age_seconds: number;
    is_stale: boolean;
    accuracy_meters: number | null;
  } | null;
  location_status: "CURRENT" | "STALE" | "UNAVAILABLE";
};

export type ContactStatusResponse = { device_id: string; generated_at: string; alerts: ContactAlert[] };

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
  "system.snapshot",
  "system.heartbeat",
  "stream.resync_required",
] as const;

/** CancelRequest.reason in backend/app/schemas.py; any other value is rejected with 422. */
export type CancelReason = "ACCIDENTAL_ACTIVATION" | "USER_CANCELLED" | "SESSION_ENDED";

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
