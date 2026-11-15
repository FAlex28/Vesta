import { apiBaseUrl } from "./config";
import type {
  AlertView,
  ApiErrorBody,
  CancelReason,
  CommandResponse,
  AccessGrantResponse,
  CompanionProfile,
  CompanionSessionResponse,
  ContactInvite,
  ContactStatusResponse,
  VoiceStatus,
  ContractTechnicalIssue,
  LocationUpdateResponse,
  ClientStatusUpdate,
  SystemStatus,
  VoiceAccessResponse,
  CamerasStatus,
  AssistantLine,
  AssistantReply,
  AssistantSpeech,
  ConversationResponse,
  ArchivedRecording,
  OperatorMessage,
  CallView,
  LocationHistory,
} from "./contract";

/*
 * HTTP client for the endpoints in shared/API_CONTRACT.md. Only those endpoints exist here.
 *
 * - The control token is passed per call and only ever placed in the Authorization header.
 *   It is never put in a URL, logged, or written to browser storage.
 * - Every command carries an event_id. A retry of the same user action must reuse the same
 *   event_id (see `newEventId`), which makes it idempotent on the server.
 */

export type ApiFailureKind =
  | "network" // the request did not reach the server or no answer arrived
  | "unauthorized"
  | "conflict"
  | "not_found"
  | "unavailable" // 503, e.g. VOICE_* errors
  | "invalid"
  | "server";

export class ApiError extends Error {
  readonly kind: ApiFailureKind;
  readonly status: number | null;
  readonly code: string | null;

  constructor(kind: ApiFailureKind, status: number | null, code: string | null, message: string) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.code = code;
  }
}

const REQUEST_TIMEOUT_MS = 8_000;

export function newEventId(prefix: string): string {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  return `${prefix}-${random}`;
}

function kindForStatus(status: number): ApiFailureKind {
  if (status === 401 || status === 403) return "unauthorized";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 422) return "invalid";
  if (status === 503) return "unavailable";
  return "server";
}

async function request<T>(
  path: string,
  init: RequestInit & { token?: string; dashboardKey?: string; timeoutMs?: number } = {},
): Promise<T> {
  const { token, dashboardKey, headers, timeoutMs, ...rest } = init;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs ?? REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl}${path}`, {
      ...rest,
      signal: controller.signal,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(typeof rest.body === "string" ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(dashboardKey ? { "X-Dashboard-Key": dashboardKey } : {}),
        ...headers,
      },
    });
  } catch {
    throw new ApiError("network", null, null, "Serverul nu a răspuns.");
  } finally {
    window.clearTimeout(timer);
  }

  if (!response.ok) {
    let code: string | null = null;
    let message = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as Partial<ApiErrorBody>;
      code = body.error?.code ?? null;
      message = body.error?.message ?? message;
    } catch {
      // Body is not the documented error shape (e.g. a proxy error page).
    }
    // A proxy in front of an unreachable backend answers 502/504: treat it as no answer.
    const kind = response.status === 502 || response.status === 504 ? "network" : kindForStatus(response.status);
    throw new ApiError(kind, response.status, code, message);
  }

  if (response.status === 204) return undefined as T;
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError("server", response.status, null, "Răspuns invalid de la server.");
  }
}

/** `keepalive` lets a report leave while the page is being hidden or closed. */
const post = <T>(path: string, body: unknown, token: string, keepalive = false) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body), token, keepalive });

const sessionPath = (sessionId: string, action: string) =>
  `/api/v1/sessions/${encodeURIComponent(sessionId)}/${action}`;

export const api = {
  status: (dashboardKey?: string) => request<SystemStatus>("/api/v1/status", { dashboardKey }),

  camerasStatus: (dashboardKey?: string) =>
    request<CamerasStatus>("/api/v1/cameras/status", { dashboardKey }),

  session: (sessionId: string, token: string) =>
    request<import("./contract").SessionView>(`/api/v1/sessions/${encodeURIComponent(sessionId)}`, { token }),

  confirmSafe: (sessionId: string, checkinId: string, token: string, eventId: string) =>
    post<CommandResponse>(sessionPath(sessionId, "checkins"), { event_id: eventId, checkin_id: checkinId, confirmation: "safe", source: "android" }, token),

  sos: (sessionId: string, token: string, eventId: string) =>
    post<CommandResponse>(sessionPath(sessionId, "sos"), { event_id: eventId, source: "android" }, token),

  /** Not valid while the session is in ALERT (409 ALERT_REQUIRES_RESOLUTION): use resolveAlert. */
  cancel: (sessionId: string, token: string, eventId: string, reason: CancelReason) =>
    post<CommandResponse>(sessionPath(sessionId, "cancel"), { event_id: eventId, reason }, token),

  resolveAlert: (sessionId: string, alertId: string, token: string, eventId: string, action: string) =>
    post<CommandResponse>(
      sessionPath(sessionId, `alerts/${encodeURIComponent(alertId)}/resolve`),
      { event_id: eventId, action },
      token,
    ),

  heartbeat: (sessionId: string, token: string) => post<CommandResponse>(sessionPath(sessionId, "heartbeat"), {}, token),

  clientStatus: (sessionId: string, token: string, eventId: string, status: ClientStatusUpdate, keepalive = false) =>
    post<CommandResponse>(sessionPath(sessionId, "client-status"), { event_id: eventId, ...status }, token, keepalive),

  voiceStatus: (
    sessionId: string,
    token: string,
    eventId: string,
    status: VoiceStatus,
    conversationId: string | null,
    keepalive = false,
  ) =>
    post<CommandResponse>(
      sessionPath(sessionId, "voice/status"),
      { event_id: eventId, status, ...(conversationId ? { conversation_id: conversationId } : {}) },
      token,
      keepalive,
    ),

  location: (sessionId: string, token: string, eventId: string, location: {
    latitude: number; longitude: number; captured_at: string; accuracy_meters: number | null; source: "android";
  }) => post<LocationUpdateResponse>(sessionPath(sessionId, "location"), { event_id: eventId, ...location }, token),

  technicalStatus: (sessionId: string, token: string, eventId: string, issue: ContractTechnicalIssue, active: boolean) =>
    post<CommandResponse>(sessionPath(sessionId, "technical-status"), { event_id: eventId, issue, active, source: "android" }, token),

  /** The phone shows the check-in prompt (channel android) or the voice agent was told (voice). */
  checkinDelivery: (sessionId: string, checkinId: string, token: string, eventId: string, channel: "android" | "voice") =>
    post<CommandResponse>(
      sessionPath(sessionId, `checkins/${encodeURIComponent(checkinId)}/delivery`),
      { event_id: eventId, status: "DELIVERED", channel },
      token,
    ),

  /** The code printed on the Vesta device (or inside its QR code), typed/scanned once by the phone. */
  pairDevice: (code: string, label: string) =>
    request<AccessGrantResponse>("/api/v1/pairing/device", { method: "POST", body: JSON.stringify({ code, label }) }),

  companionProfile: (accessToken: string) => request<CompanionProfile>("/api/v1/companion/profile", { token: accessToken }),

  updateProfile: (accessToken: string, displayName: string) =>
    request<CompanionProfile>("/api/v1/companion/profile", {
      method: "POST",
      body: JSON.stringify({ display_name: displayName }),
      token: accessToken,
    }),

  revokePhone: (accessToken: string, phoneId: string) =>
    request<CompanionProfile>(`/api/v1/companion/phones/${encodeURIComponent(phoneId)}/revoke`, { method: "POST", token: accessToken }),

  addContact: (accessToken: string, name: string, phone: string) =>
    request<ContactInvite>("/api/v1/companion/contacts", { method: "POST", body: JSON.stringify({ name, phone }), token: accessToken }),

  reinviteContact: (accessToken: string, contactId: string) =>
    request<ContactInvite>(`/api/v1/companion/contacts/${encodeURIComponent(contactId)}/invite`, { method: "POST", token: accessToken }),

  revokeContact: (accessToken: string, contactId: string) =>
    request<CompanionProfile>(`/api/v1/companion/contacts/${encodeURIComponent(contactId)}/revoke`, { method: "POST", token: accessToken }),

  /** The emergency contact opens the SMS link (or types the code) once. */
  claimContact: (code: string) =>
    request<AccessGrantResponse>("/api/v1/contact/claim", { method: "POST", body: JSON.stringify({ code }) }),

  contactPushKey: (accessToken: string) => request<{ public_key: string }>("/api/v1/contact/push/key", { token: accessToken }),

  contactPushSubscribe: (accessToken: string, subscription: PushSubscriptionJSON) =>
    request<void>("/api/v1/contact/push/subscribe", { method: "POST", body: JSON.stringify(subscription), token: accessToken }),

  contactPushUnsubscribe: (accessToken: string, endpoint: string) =>
    request<void>("/api/v1/contact/push/unsubscribe", { method: "POST", body: JSON.stringify({ endpoint }), token: accessToken }),

  /** Paired phone: the device's running session and a fresh control token, or session null. */
  companionSession: (accessToken: string) => request<CompanionSessionResponse>("/api/v1/companion/session", { token: accessToken }),

  contactStatus: (accessToken: string) => request<ContactStatusResponse>("/api/v1/contact/status", { token: accessToken }),

  contactViewed: (alertId: string, accessToken: string) =>
    request<AlertView>(`/api/v1/contact/alerts/${encodeURIComponent(alertId)}/viewed`, { method: "POST", token: accessToken }),

  /** VESTA_OPEN_ACCESS (demo): phone or trusted contact without any pairing code. */
  openAccess: (scope: "companion" | "contact") =>
    request<AccessGrantResponse>("/api/v1/access/open", { method: "POST", body: JSON.stringify({ scope }) }),

  /** Demo: the /buton page held for 5 s, instead of the joystick on the Pi. */
  simulatorPress: (eventId: string) =>
    request<{ outcome: "ACTIVATED" | "DUPLICATE" | "ALREADY_ACTIVE"; session_id: string; state: string }>(
      "/api/v1/simulator/press",
      { method: "POST", body: JSON.stringify({ event_id: eventId }) },
    ),

  /** What Vesta says now (text + optional ElevenLabs audio). The same eventId returns the same line. */
  assistantPrompt: (sessionId: string, token: string, eventId: string, line: AssistantLine) =>
    post<AssistantSpeech>(sessionPath(sessionId, "assistant/prompt"), { event_id: eventId, line }, token),

  /** One spoken phrase (WAV, base64): Gemini interprets it, the server applies the intent. */
  assistantReply: (sessionId: string, token: string, eventId: string, audioBase64: string) =>
    request<AssistantReply>(sessionPath(sessionId, "assistant/reply"), {
      method: "POST",
      body: JSON.stringify({ event_id: eventId, audio_base64: audioBase64, mime_type: "audio/wav" }),
      token,
      timeoutMs: 25_000,
    }),

  /** Chunk `seq` of the continuous conversation recording (audio/webm). */
  recordingChunk: (sessionId: string, token: string, seq: number, chunk: Blob) =>
    request<{ chunks: number; size_bytes: number }>(`${sessionPath(sessionId, "recording/chunks")}?seq=${seq}`, {
      method: "POST",
      body: chunk,
      token,
      headers: { "Content-Type": chunk.type || "audio/webm" },
      timeoutMs: 20_000,
    }),

  /** Dispatcher: every alert's recording kept in the database. */
  recordingsArchive: (dashboardKey?: string) => request<ArchivedRecording[]>("/api/v1/recordings", { dashboardKey }),

  /** Dispatcher takes the call: the assistant in her earbuds stops. */
  takeover: (sessionId: string, dashboardKey?: string) =>
    request<{ session_id: string; operator_connected_at: string }>(sessionPath(sessionId, "takeover"), { method: "POST", dashboardKey }),

  /** Dispatcher's recorded phrase (push to talk), played in her earbuds. */
  sendOperatorMessage: (sessionId: string, audio: Blob, dashboardKey?: string) =>
    request<{ id: string; at: string }>(sessionPath(sessionId, "operator/messages"), {
      method: "POST",
      body: audio,
      dashboardKey,
      headers: { "Content-Type": audio.type || "audio/webm" },
      timeoutMs: 20_000,
    }),

  /** Her phone fetches the dispatcher's phrases. */
  operatorMessages: (sessionId: string, token: string, after: string | null) =>
    request<OperatorMessage[]>(
      `${sessionPath(sessionId, "operator/messages")}${after ? `?after=${encodeURIComponent(after)}` : ""}`,
      { token },
    ),

  /** Dispatcher: the route of an alert (kept with the recording). */
  locationHistory: (sessionId: string, dashboardKey?: string) =>
    request<LocationHistory>(sessionPath(sessionId, "locations"), { dashboardKey }),

  /** Dispatcher calls her (WebRTC offer). The assistant stops; her phone answers by itself. */
  callOffer: (sessionId: string, sdp: string, dashboardKey?: string) =>
    request<CallView>(sessionPath(sessionId, "call/offer"), { method: "POST", body: JSON.stringify({ sdp }), dashboardKey }),

  /** Dispatcher waits for the phone's answer. */
  callAnswerPoll: (sessionId: string, dashboardKey?: string) =>
    request<CallView>(sessionPath(sessionId, "call/answer"), { dashboardKey }),

  callHangup: (sessionId: string, dashboardKey?: string) =>
    request<CallView>(sessionPath(sessionId, "call/hangup"), { method: "POST", dashboardKey }),

  /** Her phone: is the dispatcher calling? */
  callForPhone: (sessionId: string, token: string) => request<CallView>(sessionPath(sessionId, "call"), { token }),

  callAnswer: (sessionId: string, token: string, callId: string, sdp: string) =>
    post<CallView>(sessionPath(sessionId, "call/answer"), { call_id: callId, sdp }, token),

  /** Dispatcher: transcript and recording info, only once an alert exists. */
  conversation: (sessionId: string, dashboardKey?: string) =>
    request<ConversationResponse>(sessionPath(sessionId, "conversation"), { dashboardKey }),

  voiceAccess: (sessionId: string, token: string) =>
    post<VoiceAccessResponse>(sessionPath(sessionId, "voice/access"), { transport: "webrtc" }, token),
};
