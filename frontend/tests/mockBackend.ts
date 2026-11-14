import type { Page, Route, WebSocketRoute } from "playwright/test";
import type { EventEnvelope, SessionView, SystemStatus } from "../src/api/contract";

/*
 * In-browser mock of the v2 backend (shared/API_CONTRACT.md): GET /api/v1/status,
 * the session command endpoints and WS /ws/events. Nothing leaves the browser.
 */

export const TOKEN = "v1.test-payload.test-signature";
export const SESSION_ID = "5d9a9a55-1ca6-47bd-8768-43bcdb8492d4";

export type Recorded = { method: string; path: string; body: Record<string, unknown> | null; auth: string | null; url: string };

type Responder = (request: Recorded) => { status: number; body?: unknown } | "abort" | undefined;

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
    const [, id, action] = match;
    if (recorded.auth !== `Bearer ${TOKEN}`) {
      return route.fulfill({ status: 401, json: { error: { code: "UNAUTHORIZED", message: "Invalid control token" } } });
    }
    const session = this.session(decodeURIComponent(id));
    if (!session) return route.fulfill({ status: 404, json: { error: { code: "NOT_FOUND", message: "Unknown session" } } });

    const now = new Date().toISOString();
    const reply = () => route.fulfill({ json: { duplicate: false, session: structuredClone(session) } });

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
        session.alert_reason = "explicit_sos";
        this.transition(session, "ALERT", "safety.alerted", { reason: "explicit_sos", missed_attempts: 0, source: "android" });
        return reply();
      case "cancel":
        if (session.state === "CANCELLED") return this.conflict(route);
        this.transition(session, "CANCELLED", "session.cancelled", {});
        return reply();
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
