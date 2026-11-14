import { apiBaseUrl } from "./config";
import type {
  ApiErrorBody,
  CommandResponse,
  ContractTechnicalIssue,
  LocationUpdateResponse,
  ClientStatusUpdate,
  SystemStatus,
  VoiceAccessResponse,
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

async function request<T>(path: string, init: RequestInit & { token?: string; dashboardKey?: string } = {}): Promise<T> {
  const { token, dashboardKey, headers, ...rest } = init;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl}${path}`, {
      ...rest,
      signal: controller.signal,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(rest.body ? { "Content-Type": "application/json" } : {}),
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

  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError("server", response.status, null, "Răspuns invalid de la server.");
  }
}

const post = <T>(path: string, body: unknown, token: string) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body), token });

const sessionPath = (sessionId: string, action: string) =>
  `/api/v1/sessions/${encodeURIComponent(sessionId)}/${action}`;

export const api = {
  status: (dashboardKey?: string) => request<SystemStatus>("/api/v1/status", { dashboardKey }),

  session: (sessionId: string, token: string) =>
    request<import("./contract").SessionView>(`/api/v1/sessions/${encodeURIComponent(sessionId)}`, { token }),

  confirmSafe: (sessionId: string, checkinId: string, token: string, eventId: string) =>
    post<CommandResponse>(sessionPath(sessionId, "checkins"), { event_id: eventId, checkin_id: checkinId, confirmation: "safe", source: "android" }, token),

  sos: (sessionId: string, token: string, eventId: string) =>
    post<CommandResponse>(sessionPath(sessionId, "sos"), { event_id: eventId, source: "android" }, token),

  cancel: (sessionId: string, token: string, eventId: string, reason?: string) =>
    post<CommandResponse>(sessionPath(sessionId, "cancel"), { event_id: eventId, ...(reason ? { reason } : {}) }, token),

  heartbeat: (sessionId: string, token: string) => post<CommandResponse>(sessionPath(sessionId, "heartbeat"), {}, token),

  clientStatus: (sessionId: string, token: string, eventId: string, status: ClientStatusUpdate) =>
    post<CommandResponse>(sessionPath(sessionId, "client-status"), { event_id: eventId, ...status }, token),

  location: (sessionId: string, token: string, eventId: string, location: {
    latitude: number; longitude: number; captured_at: string; accuracy_meters: number | null; source: "android";
  }) => post<LocationUpdateResponse>(sessionPath(sessionId, "location"), { event_id: eventId, ...location }, token),

  technicalStatus: (sessionId: string, token: string, eventId: string, issue: ContractTechnicalIssue, active: boolean) =>
    post<CommandResponse>(sessionPath(sessionId, "technical-status"), { event_id: eventId, issue, active, source: "android" }, token),

  voiceAccess: (sessionId: string, token: string) =>
    post<VoiceAccessResponse>(sessionPath(sessionId, "voice/access"), { transport: "webrtc" }, token),
};
