import type { Page, Route, WebSocketRoute } from "playwright/test";
import type { EventEnvelope, SessionView, SystemStatus } from "../src/api/contract";

/*
 * In-browser mock of the v2 backend (shared/API_CONTRACT.md): GET /api/v1/status,
 * the session command endpoints and WS /ws/events. Nothing leaves the browser.
 */

export const TOKEN = "v1.test-payload.test-signature";
export const COMPANION_TOKEN = "g1.companion-grant.sig";
export const CONTACT_TOKEN = "g1.contact-grant.sig";
export const SESSION_ID = "5d9a9a55-1ca6-47bd-8768-43bcdb8492d4";

export type Recorded = { method: string; path: string; body: Record<string, unknown> | null; auth: string | null; url: string };

type Responder = (request: Recorded) => { status: number; body?: unknown } | "abort" | undefined;

/*
 * Request bodies as backend/app/schemas.py accepts them (StrictModel: extra="forbid"). A field
 * that is unknown, missing or outside its enum gets 422, exactly like the real backend.
 */
type FieldRule = { required?: boolean; oneOf?: readonly unknown[]; type?: "string" | "number" | "boolean" };
const SOURCES = ["android", "voice_agent", "dashboard", "web"] as const;
const BODY_RULES: Record<string, Record<string, FieldRule>> = {
  heartbeat: { page_visibility: { oneOf: ["VISIBLE", "HIDDEN", "UNKNOWN"] } },
  checkins: { event_id: { required: true, type: "string" }, checkin_id: { required: true, type: "string" }, confirmation: { oneOf: ["safe"] }, source: { oneOf: SOURCES } },
  sos: { event_id: { required: true, type: "string" }, source: { oneOf: SOURCES }, reason: { type: "string" } },
  cancel: { event_id: { required: true, type: "string" }, reason: { oneOf: ["ACCIDENTAL_ACTIVATION", "USER_CANCELLED", "SESSION_ENDED"] } },
  resolve: { event_id: { required: true, type: "string" }, action: { required: true, type: "string" } },
  "client-status": {
    event_id: { required: true, type: "string" },
    page_visibility: { oneOf: ["VISIBLE", "HIDDEN", "UNKNOWN"] },
    connectivity_status: { oneOf: ["CONNECTED", "RECONNECTING", "DISCONNECTED", "UNKNOWN"] },
    audio_status: { oneOf: ["CONNECTED", "DISCONNECTED", "ERROR", "UNKNOWN"] },
    gps_status: { oneOf: ["GPS_AVAILABLE", "GPS_STALE", "GPS_UNAVAILABLE"] },
    power_status: { oneOf: ["POWER_OK", "POWER_UNSTABLE", "LOW_VOLTAGE", "POWER_STATUS_UNKNOWN"] },
  },
  location: {
    event_id: { required: true, type: "string" },
    latitude: { required: true, type: "number" },
    longitude: { required: true, type: "number" },
    captured_at: { required: true, type: "string" },
    accuracy_meters: { type: "number" },
    source: { oneOf: ["android", "integration"] },
  },
  "technical-status": {
    event_id: { required: true, type: "string" },
    issue: {
      required: true,
      oneOf: ["network_disconnected", "voice_unavailable", "voice_timeout", "microphone_denied", "audio_interrupted", "audio_disconnected", "gps_stale", "gps_unavailable", "page_hidden", "low_voltage", "power_unstable", "process_restarted"],
    },
    active: { required: true, type: "boolean" },
    source: { oneOf: ["android", "voice_gateway", "backend", "integration"] },
  },
  "voice/access": { transport: { oneOf: ["webrtc"] }, participant_name: { type: "string" }, environment: { type: "string" } },
  delivery: {
    event_id: { required: true, type: "string" },
    status: { required: true, oneOf: ["DELIVERED", "UNDELIVERED"] },
    channel: { oneOf: ["voice", "web", "android"] },
  },
  "voice/status": {
    event_id: { required: true, type: "string" },
    status: { required: true, oneOf: ["CONNECTED", "DISCONNECTED", "API_ERROR", "MICROPHONE_ERROR", "TIMEOUT", "RECONNECTING", "RECONNECTED", "SERVICE_UNAVAILABLE"] },
    conversation_id: { type: "string" },
  },
};

export function bodyProblem(action: string, body: Record<string, unknown> | null): string | null {
  const rules = BODY_RULES[action];
  if (!rules) return null;
  const data = body ?? {};
  for (const key of Object.keys(data)) if (!(key in rules)) return `extra field ${key}`;
  for (const [key, rule] of Object.entries(rules)) {
    const value = data[key];
    if (value === undefined || value === null) {
      if (rule.required) return `missing ${key}`;
      continue;
    }
    if (rule.type && typeof value !== rule.type) return `${key} must be ${rule.type}`;
    if (rule.oneOf && !rule.oneOf.includes(value)) return `${key} not allowed: ${String(value)}`;
  }
  if (action === "client-status" && Object.keys(data).filter((key) => key !== "event_id").length === 0) return "no status";
  return null;
}

export function makeSession(overrides: Partial<SessionView> = {}): SessionView {
  const now = new Date().toISOString();
  return {
    id: SESSION_ID,
    device_id: "vesta-01",
    state: "ACTIVE",
    created_at: new Date(Date.now() - 65_000).toISOString(),
    updated_at: now,
    health_status: "HEALTHY",
    version: 1,
    quick_cancel_until: new Date(Date.now() + 30_000).toISOString(),
    cancellation_reason: null,
    checkin_attempt: 0,
    missed_attempts: 0,
    undelivered_attempts: 0,
    checkin_id: null,
    checkin_status: null,
    checkin_requested_at: null,
    checkin_delivered_at: null,
    last_checkin_id: null,
    last_checkin_status: null,
    last_confirmation_at: null,
    next_checkin_at: new Date(Date.now() + 60_000).toISOString(),
    response_deadline: null,
    retry_at: null,
    client_last_seen_at: null,
    connection_lease_expires_at: null,
    active_technical_issues: [],
    alert_reason: null,
    page_visibility: "UNKNOWN",
    connectivity_status: "UNKNOWN",
    audio_status: "UNKNOWN",
    gps_status: "GPS_UNAVAILABLE",
    power_status: "POWER_STATUS_UNKNOWN",
    alert: null,
    ...overrides,
  };
}

export class MockBackend {
  sessions: SessionView[] = [];
  sequence = 0;
  voiceMode: SystemStatus["voice_mode"] = "elevenlabs";
  requests: Recorded[] = [];
  sockets: WebSocketRoute[] = [];
  /** Per-path overrides; return undefined to fall through to the default behaviour. */
  responders: Responder[] = [];
  acceptSockets = true;
  /** The backend's one-conversation-per-session reservation. */
  voiceReserved = false;
  /** Code printed on the device (QR label); reusable, unlike invites. */
  deviceCode = "VESTA23456";
  ownerName: string | null = null;
  phones: Array<{ id: string; label: string; created_at: string; last_seen_at: string }> = [];
  contacts: Array<{ id: string; name: string; phone: string; created_at: string; accepted_at: string | null; push_enabled: boolean; invite_pending: boolean }> = [];
  /** One-time contact invites: code -> contact id. */
  invites = new Map<string, string>();
  /** Last authorized location per session, as the real backend keeps it. */
  locations = new Map<string, { latitude: number; longitude: number; captured_at: string; accuracy_meters: number | null }>();

  /** An invite for a new contact, as if the person had added one on her phone. */
  inviteContact(name = "Mama", phone = "+40722123456") {
    const id = `ct-${this.contacts.length + 1}`;
    this.contacts.push({ id, name, phone, created_at: new Date().toISOString(), accepted_at: null, push_enabled: false, invite_pending: true });
    const code = `INV${String(this.contacts.length).padStart(5, "7")}`;
    this.invites.set(code, id);
    return code;
  }

  private profile() {
    return { device_id: "vesta-01", display_name: this.ownerName, this_phone_id: this.phones[0]?.id ?? "ph-1", phones: this.phones, contacts: this.contacts };
  }

  private eventCounter = 0;

  constructor(private page: Page) {}

  status(): SystemStatus {
    const live = this.sessions.filter((session) => session.state !== "CANCELLED").map((session) => session.state);
    const order = ["ALERT", "UNCONFIRMED", "CHECK_IN", "ACTIVE"] as const;
    return {
      system_state: order.find((state) => live.includes(state)) ?? "IDLE",
      generated_at: new Date().toISOString(),
      latest_sequence: this.sequence,
      sessions: structuredClone(this.sessions),
      voice_mode: this.voiceMode,
      alert_locations: this.sessions
        .filter((item) => item.alert?.status === "ACTIVE" && this.locations.has(item.id))
        .map((item) => {
          const location = this.locations.get(item.id)!;
          const age = (Date.now() - Date.parse(location.captured_at)) / 1000;
          return { session_id: item.id, ...location, age_seconds: age, is_stale: age > 60 };
        }),
    };
  }

  session(id = SESSION_ID) {
    return this.sessions.find((session) => session.id === id);
  }

  async install() {
    await this.page.route("**/api/v1/**", (route) => this.handle(route));
    await this.page.routeWebSocket(/\/ws\/events$/, (ws) => {
      if (!this.acceptSockets) {
        void ws.close({ code: 1011, reason: "unavailable" });
        return;
      }
      this.sockets.push(ws);
      ws.onClose(() => {
        this.sockets = this.sockets.filter((item) => item !== ws);
      });
      ws.send(
        JSON.stringify({
          event_id: `snapshot-${++this.eventCounter}`,
          sequence: this.sequence,
          type: "system.snapshot",
          occurred_at: new Date().toISOString(),
          session_id: null,
          device_id: null,
          data: this.status(),
        }),
      );
    });
  }

  private async handle(route: Route) {
    const request = route.request();
    const url = new URL(request.url());
    let body: Record<string, unknown> | null = null;
    try {
      body = request.postDataJSON() as Record<string, unknown>;
    } catch {
      body = null;
    }
    const recorded: Recorded = {
      method: request.method(),
      path: url.pathname,
      body,
      auth: (await request.allHeaders()).authorization ?? null,
      url: request.url(),
    };
    this.requests.push(recorded);

    for (const responder of this.responders) {
      const result = responder(recorded);
      if (result === "abort") return route.abort("connectionfailed");
      if (result) return route.fulfill({ status: result.status, json: result.body ?? {} });
    }

    if (recorded.path === "/api/v1/status") return route.fulfill({ json: this.status() });

    const unauthorized = () => route.fulfill({ status: 401, json: { error: { code: "UNAUTHORIZED", message: "Invalid or missing authorization" } } });
    const normalize = (value: unknown) => String(value ?? "").toUpperCase().replace(/[^A-Z0-9]/g, "");
    const grant = (scope: "companion" | "contact") =>
      route.fulfill({
        json: { scope, device_id: "vesta-01", access_token: scope === "companion" ? COMPANION_TOKEN : CONTACT_TOKEN, expires_at: new Date(Date.now() + 86_400_000).toISOString() },
      });
    if (recorded.path === "/api/v1/pairing/device" && recorded.method === "POST") {
      if (normalize(body?.code) !== this.deviceCode) return unauthorized();
      const now = new Date().toISOString();
      this.phones.push({ id: `ph-${this.phones.length + 1}`, label: String(body?.label ?? "Telefon"), created_at: now, last_seen_at: now });
      return grant("companion");
    }
    if (recorded.path === "/api/v1/contact/claim" && recorded.method === "POST") {
      const code = normalize(body?.code);
      const contactId = this.invites.get(code);
      if (!contactId) return unauthorized();
      this.invites.delete(code);
      const contact = this.contacts.find((item) => item.id === contactId)!;
      contact.accepted_at = new Date().toISOString();
      contact.invite_pending = false;
      return grant("contact");
    }
    if (recorded.path.startsWith("/api/v1/companion/")) {
      if (recorded.auth !== `Bearer ${COMPANION_TOKEN}`) return unauthorized();
      if (recorded.path === "/api/v1/companion/session") {
        const live = this.sessions.find((item) => item.state !== "CANCELLED");
        return route.fulfill({
          json: live
            ? { device_id: "vesta-01", session: structuredClone(live), control_token: TOKEN, control_token_expires_at: new Date(Date.now() + 3_600_000).toISOString() }
            : { device_id: "vesta-01", session: null, control_token: null, control_token_expires_at: null },
        });
      }
      if (recorded.path === "/api/v1/companion/profile") {
        if (recorded.method === "POST") this.ownerName = (body?.display_name as string | null) ?? null;
        return route.fulfill({ json: this.profile() });
      }
      if (recorded.path === "/api/v1/companion/contacts" && recorded.method === "POST") {
        if (!/^\+?[0-9 ()-]{6,}$/.test(String(body?.phone ?? ""))) return route.fulfill({ status: 422, json: { error: { code: "INVALID_INPUT", message: "phone" } } });
        const code = this.inviteContact(String(body?.name), String(body?.phone).replace(/[^0-9+]/g, ""));
        const contact = this.contacts[this.contacts.length - 1];
        return route.fulfill({ json: { contact, code, expires_at: new Date(Date.now() + 86_400_000).toISOString() } });
      }
      const contactAction = recorded.path.match(/^\/api\/v1\/companion\/contacts\/([^/]+)\/(invite|revoke)$/);
      if (contactAction) {
        const [, id, what] = contactAction;
        if (what === "revoke") {
          this.contacts = this.contacts.filter((item) => item.id !== id);
          return route.fulfill({ json: this.profile() });
        }
        const code = `RE${String(Date.now()).slice(-6)}`;
        this.invites.set(code, id);
        return route.fulfill({ json: { contact: this.contacts.find((item) => item.id === id), code, expires_at: new Date(Date.now() + 86_400_000).toISOString() } });
      }
      const phoneAction = recorded.path.match(/^\/api\/v1\/companion\/phones\/([^/]+)\/revoke$/);
      if (phoneAction) {
        this.phones = this.phones.filter((item) => item.id !== phoneAction[1]);
        return route.fulfill({ json: this.profile() });
      }
    }
    if (recorded.path === "/api/v1/contact/push/key") {
      if (recorded.auth !== `Bearer ${CONTACT_TOKEN}`) return unauthorized();
      return route.fulfill({ json: { public_key: "BMx7test-public-key" } });
    }
    if (recorded.path === "/api/v1/contact/status") {
      if (recorded.auth !== `Bearer ${CONTACT_TOKEN}`) return unauthorized();
      const alerts = this.sessions
        .filter((item) => item.alert)
        .map((item) => {
          const location = item.alert!.status === "ACTIVE" ? this.locations.get(item.id) : undefined;
          const age = location ? (Date.now() - Date.parse(location.captured_at)) / 1000 : 0;
          return {
            session_id: item.id,
            state: item.state,
            alert_reason: item.alert_reason,
            alert: structuredClone(item.alert),
            location: location ? { ...location, age_seconds: age, is_stale: age > 60 } : null,
            location_status: location ? (age > 60 ? "STALE" : "CURRENT") : "UNAVAILABLE",
            push_status: null,
          };
        });
      return route.fulfill({
        json: { device_id: "vesta-01", generated_at: new Date().toISOString(), person_name: this.ownerName, contact_name: "Mama", push_enabled: false, alerts },
      });
    }
    const viewed = recorded.path.match(/^\/api\/v1\/contact\/alerts\/([^/]+)\/viewed$/);
    if (viewed && recorded.method === "POST") {
      if (recorded.auth !== `Bearer ${CONTACT_TOKEN}`) return unauthorized();
      const owner = this.sessions.find((item) => item.alert?.id === decodeURIComponent(viewed[1]));
      if (!owner?.alert) return route.fulfill({ status: 404, json: { error: { code: "NOT_FOUND", message: "Alert not found" } } });
      owner.alert.viewed_at = owner.alert.viewed_at ?? new Date().toISOString();
      return route.fulfill({ json: structuredClone(owner.alert) });
    }

    const sessionRead = recorded.path.match(/^\/api\/v1\/sessions\/([^/]+)$/);
    if (recorded.method === "GET" && sessionRead) {
      if (recorded.auth !== `Bearer ${TOKEN}`) {
        return route.fulfill({ status: 401, json: { error: { code: "UNAUTHORIZED", message: "Invalid control token" } } });
      }
      const session = this.session(decodeURIComponent(sessionRead[1]));
      return session
        ? route.fulfill({ json: structuredClone(session) })
        : route.fulfill({ status: 404, json: { error: { code: "NOT_FOUND", message: "Unknown session" } } });
    }

    const match = recorded.path.match(/^\/api\/v1\/sessions\/([^/]+)\/(.+)$/);
    if (!match) return route.fulfill({ status: 404, json: { error: { code: "NOT_FOUND", message: "not found" } } });
    const [, id, rawAction] = match;
    const resolveMatch = rawAction.match(/^alerts\/([^/]+)\/resolve$/);
    const deliveryMatch = rawAction.match(/^checkins\/([^/]+)\/delivery$/);
    const action = resolveMatch ? "resolve" : deliveryMatch ? "delivery" : rawAction;
    if (recorded.auth !== `Bearer ${TOKEN}`) {
      return route.fulfill({ status: 401, json: { error: { code: "UNAUTHORIZED", message: "Invalid control token" } } });
    }
    const session = this.session(decodeURIComponent(id));
    if (!session) return route.fulfill({ status: 404, json: { error: { code: "NOT_FOUND", message: "Unknown session" } } });

    const problem = bodyProblem(action, body);
    if (problem) return route.fulfill({ status: 422, json: { error: { code: "VALIDATION_ERROR", message: problem } } });

    const now = new Date().toISOString();
    const reply = () => route.fulfill({ json: { duplicate: false, session: structuredClone(session) } });
    const conflict = (code: string) => route.fulfill({ status: 409, json: { error: { code, message: code } } });

    switch (action) {
      case "heartbeat":
        session.client_last_seen_at = now;
        session.connection_lease_expires_at = new Date(Date.now() + 20_000).toISOString();
        return reply();
      case "checkins":
        if ((session.state !== "CHECK_IN" && session.state !== "UNCONFIRMED") || body?.checkin_id !== session.checkin_id) return this.conflict(route);
        this.transition(session, "ACTIVE", "safety.check_in_confirmed", { source: "android" });
        session.checkin_attempt = 0;
        session.missed_attempts = 0;
        session.response_deadline = null;
        session.last_checkin_id = session.checkin_id;
        session.last_checkin_status = "CONFIRMED";
        session.last_confirmation_at = now;
        session.checkin_id = null;
        session.checkin_status = "CONFIRMED";
        return reply();
      case "sos":
        if (!["ACTIVE", "CHECK_IN", "UNCONFIRMED"].includes(session.state)) return this.conflict(route);
        session.alert_reason = "SOS_EXPLICIT";
        session.alert = {
          id: `alert-${session.id}`,
          incident_type: "EXPLICIT_SOS",
          status: "ACTIVE",
          created_at: now,
          updated_at: now,
          notification_requested_at: now,
          resolved_at: null,
          resolution_action: null,
          version: 1,
        };
        this.transition(session, "ALERT", "safety.alerted", { reason: "SOS_EXPLICIT", missed_attempts: 0, source: "android" });
        this.publish("alert.notification_requested", { alert_id: session.alert.id, incident_type: "EXPLICIT_SOS", gps_status: session.gps_status, location: null, location_available: false });
        return reply();
      case "cancel": {
        const reason = String(body?.reason ?? "USER_CANCELLED");
        if (session.state === "CANCELLED") return this.conflict(route);
        if (reason === "ACCIDENTAL_ACTIVATION" && !(session.quick_cancel_until && Date.parse(session.quick_cancel_until) > Date.now())) {
          return conflict("QUICK_CANCEL_EXPIRED");
        }
        if (session.state === "ALERT" && reason !== "ACCIDENTAL_ACTIVATION") return conflict("ALERT_REQUIRES_RESOLUTION");
        session.cancellation_reason = reason;
        this.transition(session, "CANCELLED", "session.cancelled", { reason });
        return reply();
      }
      case "resolve": {
        const alertId = decodeURIComponent(resolveMatch![1]);
        if (!session.alert || session.alert.id !== alertId) return route.fulfill({ status: 404, json: { error: { code: "ALERT_NOT_FOUND", message: "unknown alert" } } });
        if (session.alert.status === "RESOLVED") return conflict("ALERT_ALREADY_RESOLVED");
        session.alert = { ...session.alert, status: "RESOLVED", resolved_at: now, resolution_action: String(body?.action), updated_at: now, version: session.alert.version + 1 };
        session.cancellation_reason = "ALERT_RESOLVED";
        this.transition(session, "CANCELLED", "safety.alert_resolved", { alert_id: alertId, resolution_action: body?.action, resolved_at: now });
        this.publish("alert.resolved", { alert_id: alertId, status: "RESOLVED", resolution_action: body?.action, resolved_at: now });
        return reply();
      }
      case "delivery": {
        const checkinId = decodeURIComponent(deliveryMatch![1]);
        if (session.state !== "CHECK_IN" || session.checkin_id !== checkinId) return conflict("CHECKIN_MISMATCH");
        if (session.checkin_status !== "REQUESTED") return conflict("CHECKIN_ALREADY_REPORTED");
        session.checkin_status = "DELIVERED";
        session.checkin_delivered_at = now;
        this.publish("safety.check_in_delivered", { from_state: "CHECK_IN", state: "CHECK_IN", checkin_id: checkinId, channel: body?.channel });
        return reply();
      }
      case "client-status": {
        const fields = ["page_visibility", "connectivity_status", "audio_status", "gps_status", "power_status"] as const;
        for (const field of fields) if (typeof body?.[field] === "string") (session as Record<string, unknown>)[field] = body[field];
        session.updated_at = now;
        this.publish("client.status_changed", { ...Object.fromEntries(fields.map((field) => [field, session[field]])), state: session.state, source: "android" });
        return reply();
      }
      case "voice/status": {
        const status = String(body?.status);
        this.voiceReserved = ["CONNECTED", "RECONNECTING", "RECONNECTED"].includes(status);
        session.audio_status = status === "CONNECTED" || status === "RECONNECTED" ? "CONNECTED" : status === "DISCONNECTED" ? "DISCONNECTED" : status === "RECONNECTING" ? "UNKNOWN" : "ERROR";
        session.updated_at = now;
        this.publish("voice.status_changed", { status, state: session.state });
        return reply();
      }
      case "technical-status": {
        const issue = String(body?.issue);
        if (body?.active) {
          if (!session.active_technical_issues.includes(issue)) session.active_technical_issues.push(issue);
        } else {
          session.active_technical_issues = session.active_technical_issues.filter((item) => item !== issue);
        }
        return reply();
      }
      case "location":
        this.locations.set(session.id, {
          latitude: Number(body?.latitude),
          longitude: Number(body?.longitude),
          captured_at: String(body?.captured_at),
          accuracy_meters: typeof body?.accuracy_meters === "number" ? body.accuracy_meters : null,
        });
        return route.fulfill({
          json: {
            duplicate: false,
            accepted_as_latest: true,
            location: {
              latitude: body?.latitude,
              longitude: body?.longitude,
              captured_at: body?.captured_at,
              received_at: now,
              accuracy_meters: body?.accuracy_meters,
              source: body?.source,
            },
          },
        });
      case "voice/access":
        if (session.state === "CANCELLED") return conflict("SESSION_CANCELLED");
        if (this.voiceReserved) return conflict("VOICE_ALREADY_ACTIVE");
        this.voiceReserved = true;
        return route.fulfill({
          json:
            this.voiceMode === "demo"
              ? { provider: "demo", mode: "demo", transport: "scripted", conversation_token: null, conversation_id: `demo-${id}`, client_config: { language: "ro", demo_messages: ["Bună, sunt Vesta.", "Ești în regulă?"] } }
              : { provider: "elevenlabs", mode: "live", transport: "webrtc", conversation_token: "temporary-token", conversation_id: "conv_123", client_config: { language: "ro", session_id: id } },
        });
      default:
        return route.fulfill({ status: 404, json: { error: { code: "NOT_FOUND", message: "unknown" } } });
    }
  }

  private conflict(route: Route) {
    return route.fulfill({ status: 409, json: { error: { code: "INVALID_SESSION_TRANSITION", message: "invalid" } } });
  }

  /** Apply a state change on the mock and publish the matching event on every socket. */
  transition(session: SessionView, state: SessionView["state"], type: string, data: Record<string, unknown>) {
    const from = session.state;
    session.state = state;
    session.updated_at = new Date().toISOString();
    return this.publish(type, { from_state: from, state, ...data }, session.id);
  }

  event(type: string, data: Record<string, unknown>, sessionId: string | null = SESSION_ID, eventId?: string, sequence?: number): EventEnvelope {
    return {
      event_id: eventId ?? `srv-${++this.eventCounter}`,
      sequence: sequence ?? ++this.sequence,
      type,
      occurred_at: new Date().toISOString(),
      session_id: sessionId,
      device_id: sessionId ? "vesta-01" : null,
      data,
    };
  }

  publish(type: string, data: Record<string, unknown>, sessionId: string | null = SESSION_ID) {
    const envelope = this.event(type, data, sessionId);
    this.send(envelope);
    return envelope;
  }

  send(envelope: EventEnvelope) {
    for (const socket of this.sockets) socket.send(JSON.stringify(envelope));
  }

  async dropSockets() {
    const sockets = [...this.sockets];
    this.sockets = [];
    await Promise.all(sockets.map((socket) => socket.close({ code: 1006 }).catch(() => undefined)));
  }

  calls(path: string | RegExp) {
    return this.requests.filter((request) => (typeof path === "string" ? request.path === path : path.test(request.path)));
  }
}

/** Replace browser device APIs before the app loads. No real permission prompt is triggered. */
export async function installDeviceFakes(
  page: Page,
  options: { mic?: "grant" | "deny"; gps?: "grant" | "deny" | "stale" } = {},
) {
  await page.addInitScript(({ mic, gps }) => {
    const w = window as unknown as Record<string, unknown>;
    w.__voiceLog = [] as string[];
    if (navigator.mediaDevices) {
      navigator.mediaDevices.getUserMedia = async () => {
        if (mic === "deny") throw new DOMException("denied", "NotAllowedError");
        return { getTracks: () => [{ stop() {} }] } as unknown as MediaStream;
      };
    }
    const position = (ageMs: number) =>
      ({
        coords: { latitude: 45.75, longitude: 21.23, accuracy: 18, altitude: null, altitudeAccuracy: null, heading: null, speed: null },
        timestamp: Date.now() - ageMs,
      }) as unknown as GeolocationPosition;
    const deny = (error?: PositionErrorCallback | null) =>
      error?.({ code: 1, message: "denied", PERMISSION_DENIED: 1, POSITION_UNAVAILABLE: 2, TIMEOUT: 3 } as GeolocationPositionError);
    Object.defineProperty(navigator, "geolocation", {
      configurable: true,
      value: {
        watchPosition(success: PositionCallback, error?: PositionErrorCallback | null) {
          setTimeout(() => (gps === "deny" ? deny(error) : success(position(gps === "stale" ? 120_000 : 0))), 50);
          return 1;
        },
        getCurrentPosition(success: PositionCallback, error?: PositionErrorCallback | null) {
          setTimeout(() => (gps === "deny" ? deny(error) : success(position(gps === "stale" ? 120_000 : 0))), 50);
        },
        clearWatch() {},
      },
    });
    // Fake ElevenLabs SDK: records calls and lets the test drive connection events.
    w.__vestaVoiceFactory = async (options: Record<string, (arg?: unknown) => void>) => {
      w.__voiceOptions = options;
      setTimeout(() => options.onConnect?.({ conversationId: "conv_123" }), 30);
      return {
        endSession: async () => {
          (w.__voiceLog as string[]).push("end");
          options.onDisconnect?.({ reason: "user" });
        },
        sendContextualUpdate: (text: string) => (w.__voiceLog as string[]).push(`context:${text}`),
      };
    };
  }, options);
}
