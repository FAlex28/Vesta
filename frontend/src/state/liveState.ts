import {
  type AlertLocation,
  isUnfinished,
  SESSION_STATES,
  type AlertReason,
  type CommandResponse,
  type EventEnvelope,
  type SessionState,
  type SessionView,
  type SystemStatus,
} from "../api/contract";

/*
 * Pure reducer for everything the server tells us. The server is authoritative:
 *
 * - A snapshot (GET /api/v1/status or the WebSocket's first `system.snapshot`) replaces all
 *   sessions. Sessions that disappear after a restart are reported as interrupted, never
 *   restored from local memory.
 * - Two watermarks. `lastSequence` belongs to the WebSocket stream: an event at or below it,
 *   or with an event_id already seen, is ignored (dedupe) and the rest are logged.
 *   `stateSequence` is the newest server state reflected in `sessions`: a GET /status can
 *   overtake events still in flight on the socket, and those events are then logged but not
 *   re-applied, so they cannot roll the state back.
 * - Missed events are marked only when a reconnected stream's snapshot skips ahead.
 * - An event that cannot be applied exactly (unknown session, sequence gap, fields the event
 *   does not carry) sets `resyncNeeded`; the caller then refreshes from GET /status.
 * - Alert records use the v2 alert id and timestamps from SessionView. A session may still
 *   have only one alert, but the server's identity remains authoritative.
 */

export type LogEntry = {
  id: string;
  sequence: number;
  type: string;
  occurredAt: string;
  sessionId: string | null;
  data: Record<string, unknown>;
};

export type GapEntry = { id: string; type: "client.gap"; occurredAt: string; missed: number };

export type AlertRecord = {
  id: string;
  sessionId: string;
  deviceId: string;
  status: "ALERT" | "RESOLVED";
  reason: AlertReason | null;
  incidentType: string | null;
  raisedAt: string | null;
  resolvedAt: string | null;
  resolutionAction: string | null;
};

/**
 * What `alert.notification_requested` said about location when the alert was transmitted.
 * Coordinates are deliberately not kept: this frontend has no authorized private view for them.
 */
export type AlertLocationNotice = {
  alertId: string | null;
  at: string;
  gpsStatus: string | null;
  locationAvailable: boolean;
  capturedAt: string | null;
  ageSeconds: number | null;
  isStale: boolean | null;
};

/** Raspberry Pi power as reported by `technical.power` (no battery percentage exists). */
export type SystemPower = { state: string; detail: string | null; at: string };

export type LiveState = {
  sessions: Record<string, SessionView>;
  /** Dispatcher: last authorized location per session with an ACTIVE alert (from status). */
  alertLocations: Record<string, AlertLocation>;
  alertNotices: Record<string, AlertLocationNotice>;
  systemPower: SystemPower | null;
  voiceMode: SystemStatus["voice_mode"] | null;
  systemHealth: SystemStatus["system_health"] | null;
  /** generated_at of the last applied snapshot. Null until the server has answered once. */
  snapshotAt: string | null;
  /** Stream watermark (dedupe + log). */
  lastSequence: number;
  /** Newest server sequence reflected in `sessions`. */
  stateSequence: number;
  /** A stream snapshot was received at least once (reconnects can then detect gaps). */
  streamSeen: boolean;
  seen: string[];
  log: Array<LogEntry | GapEntry>;
  alerts: Record<string, AlertRecord>;
  /** Only known from `safety.check_in_confirmed` events received by this client. */
  lastConfirmedAt: Record<string, string>;
  /** Sessions known before a snapshot that no longer contains them (e.g. server restart). */
  interrupted: Record<string, SessionView>;
  restartDetected: boolean;
  resyncNeeded: boolean;
};

export type LiveAction =
  | { type: "snapshot"; status: SystemStatus; source?: "http" | "stream" }
  | { type: "sessionSnapshot"; session: SessionView; receivedAt: string }
  | { type: "event"; event: EventEnvelope }
  | { type: "command"; response: CommandResponse }
  | { type: "acknowledgeRestart" };

const MAX_SEEN = 500;
const MAX_LOG = 200;

export const initialLiveState: LiveState = {
  sessions: {},
  alertLocations: {},
  alertNotices: {},
  systemPower: null,
  voiceMode: null,
  systemHealth: null,
  snapshotAt: null,
  lastSequence: 0,
  stateSequence: 0,
  streamSeen: false,
  seen: [],
  log: [],
  alerts: {},
  lastConfirmedAt: {},
  interrupted: {},
  restartDetected: false,
  resyncNeeded: false,
};

function withGap(log: LiveState["log"], id: string, occurredAt: string, missed: number): LiveState["log"] {
  if (log.some((entry) => entry.id === id)) return log;
  return [{ id, type: "client.gap" as const, occurredAt, missed }, ...log].slice(0, MAX_LOG);
}

const isState = (value: unknown): value is SessionState =>
  typeof value === "string" && (SESSION_STATES as readonly string[]).includes(value);

const time = (iso: string | null | undefined) => (iso ? Date.parse(iso) : Number.NaN);

const str = (value: unknown) => (typeof value === "string" ? value : null);

/** Data minimisation: coordinates in an event are never kept in the client log. */
function loggable(data: Record<string, unknown>): Record<string, unknown> {
  if (!("location" in data)) return data;
  const { location, ...rest } = data;
  return { ...rest, location_available: location !== null && location !== undefined };
}

const CLIENT_STATUS_FIELDS = ["page_visibility", "connectivity_status", "audio_status", "gps_status", "power_status", "health_status"] as const;

function alertFromSession(session: SessionView, previous: AlertRecord | undefined): AlertRecord | undefined {
  const serverAlert = session.alert;
  if (serverAlert) {
    return {
      id: serverAlert.id,
      sessionId: session.id,
      deviceId: session.device_id,
      status: serverAlert.status === "ACTIVE" ? "ALERT" : "RESOLVED",
      reason: session.alert_reason as AlertReason | null,
      incidentType: serverAlert.incident_type,
      raisedAt: serverAlert.created_at,
      resolvedAt: serverAlert.resolved_at,
      resolutionAction: serverAlert.resolution_action,
    };
  }
  if (session.state === "ALERT") {
    return {
      id: previous?.id ?? `session:${session.id}`,
      sessionId: session.id,
      deviceId: session.device_id,
      status: "ALERT",
      reason: (session.alert_reason as AlertReason | null) ?? previous?.reason ?? null,
      incidentType: previous?.incidentType ?? null,
      raisedAt: previous?.raisedAt ?? null,
      resolvedAt: null,
      resolutionAction: null,
    };
  }
  if (session.state === "CANCELLED" && (session.alert_reason || previous)) {
    return {
      sessionId: session.id,
      id: previous?.id ?? `session:${session.id}`,
      deviceId: session.device_id,
      status: "RESOLVED",
      reason: (session.alert_reason as AlertReason | null) ?? previous?.reason ?? null,
      incidentType: previous?.incidentType ?? null,
      raisedAt: previous?.raisedAt ?? null,
      // Cancellation is the last change a session can receive, so updated_at is its time.
      resolvedAt: previous?.resolvedAt ?? session.updated_at,
      resolutionAction: previous?.resolutionAction ?? null,
    };
  }
  return previous;
}

function withSession(state: LiveState, session: SessionView): LiveState {
  const alert = alertFromSession(session, state.alerts[session.id]);
  return {
    ...state,
    sessions: { ...state.sessions, [session.id]: session },
    alerts: alert ? { ...state.alerts, [session.id]: alert } : state.alerts,
  };
}

function applySnapshot(state: LiveState, status: SystemStatus, source: "http" | "stream"): LiveState {
  // An older answer must not overwrite newer knowledge (e.g. a slow GET after a WS snapshot).
  // A stream snapshot still sets that stream's watermark.
  if (state.snapshotAt && time(status.generated_at) < time(state.snapshotAt)) {
    return source === "stream" ? { ...state, lastSequence: status.latest_sequence, streamSeen: true } : state;
  }

  const restart = state.snapshotAt !== null && status.latest_sequence < Math.max(state.stateSequence, state.lastSequence);
  const incoming = new Set(status.sessions.map((session) => session.id));
  const interrupted = { ...state.interrupted };
  for (const [id, session] of Object.entries(state.sessions)) {
    if (!incoming.has(id) && isUnfinished(session.state)) interrupted[id] = session;
  }

  let next: LiveState = {
    ...state,
    sessions: {},
    voiceMode: status.voice_mode,
    systemHealth: status.system_health,
    alertLocations: Object.fromEntries((status.alert_locations ?? []).map((item) => [item.session_id, item])),
    systemPower: status.power
      ? { state: status.power.state, detail: status.power.detail, at: status.power.checked_at }
      : state.systemPower,
    snapshotAt: status.generated_at,
    stateSequence: status.latest_sequence,
    lastSequence: source === "stream" || restart ? status.latest_sequence : state.lastSequence,
    streamSeen: state.streamSeen || source === "stream",
    interrupted,
    restartDetected: state.restartDetected || restart,
    resyncNeeded: false,
    // After a restart the server's sequence numbers start again; old ids cannot collide.
    seen: restart ? [] : state.seen,
  };

  // A reconnected stream starts at its snapshot: events in between were never received.
  const missed = status.latest_sequence - state.lastSequence;
  if (source === "stream" && state.streamSeen && !restart && missed > 0) {
    next.log = withGap(next.log, `gap-${status.latest_sequence}`, status.generated_at, missed);
  }

  // Alerts for sessions that vanished keep their last known record; they are not deleted.
  for (const session of status.sessions) next = withSession(next, session);
  return next;
}

function applyEvent(state: LiveState, event: EventEnvelope): LiveState {
  if (event.type === "system.heartbeat") return state;
  if (event.type === "system.snapshot") {
    const status = event.data as unknown as SystemStatus;
    return Array.isArray(status?.sessions) ? applySnapshot(state, status, "stream") : { ...state, resyncNeeded: true };
  }
  if (event.type === "stream.resync_required" || event.type === "system.recovered") {
    return { ...state, resyncNeeded: true };
  }

  if (state.seen.includes(event.event_id) || event.sequence <= state.lastSequence) return state;

  const gap = state.streamSeen && event.sequence > state.lastSequence + 1;
  const gapLog = gap
    ? withGap(state.log, `gap-${event.sequence}`, event.occurred_at, event.sequence - state.lastSequence - 1)
    : state.log;
  // `seen` is cleared after a restart; the log itself never holds the same event_id twice.
  const baseLog = gapLog.filter((entry) => entry.id !== event.event_id);
  // The phone's routine heartbeat republishes its status every few seconds: apply, do not log.
  const routine = event.type === "client.status_changed" && event.data?.source === "client_heartbeat";
  const next: LiveState = {
    ...state,
    lastSequence: event.sequence,
    seen: [...state.seen, event.event_id].slice(-MAX_SEEN),
    log: routine
      ? baseLog
      : [
          {
            id: event.event_id,
            sequence: event.sequence,
            type: event.type,
            occurredAt: event.occurred_at,
            sessionId: event.session_id,
            data: loggable(event.data ?? {}),
          },
          ...baseLog,
        ].slice(0, MAX_LOG),
    resyncNeeded: state.resyncNeeded || gap || state.snapshotAt === null,
  };

  const sessionId = event.session_id;
  const data = event.data ?? {};

  if (event.type === "safety.check_in_confirmed" && sessionId) {
    const known = next.lastConfirmedAt[sessionId];
    if (!known || time(known) < time(event.occurred_at)) {
      next.lastConfirmedAt = { ...next.lastConfirmedAt, [sessionId]: event.occurred_at };
    }
  }

  // Facts that GET /status cannot return later: keep them even if the state already moved on.
  if (event.type === "alert.notification_requested" && sessionId) {
    const location = data.location && typeof data.location === "object" ? (data.location as Record<string, unknown>) : null;
    next.alertNotices = {
      ...next.alertNotices,
      [sessionId]: {
        alertId: str(data.alert_id),
        at: str(data.notification_requested_at) ?? event.occurred_at,
        gpsStatus: str(data.gps_status),
        locationAvailable: data.location_available === true && location !== null,
        capturedAt: location ? str(location.captured_at) : null,
        ageSeconds: location && typeof location.age_seconds === "number" ? location.age_seconds : null,
        isStale: location && typeof location.is_stale === "boolean" ? location.is_stale : null,
      },
    };
  }
  if (event.type === "technical.power" && typeof data.state === "string") {
    next.systemPower = { state: data.state, detail: str(data.detail), at: event.occurred_at };
  }

  // Already reflected by a newer status answer: keep the facts (times), not the transition.
  if (event.sequence <= state.stateSequence) {
    const previous = sessionId ? next.alerts[sessionId] : undefined;
    if (previous && event.type === "safety.alerted" && !previous.raisedAt) {
      next.alerts = { ...next.alerts, [previous.sessionId]: { ...previous, raisedAt: event.occurred_at } };
    }
    if (previous && event.type === "session.cancelled" && previous.status === "RESOLVED") {
      next.alerts = { ...next.alerts, [previous.sessionId]: { ...previous, resolvedAt: event.occurred_at } };
    }
    return next;
  }
  next.stateSequence = event.sequence;
  const current = sessionId ? next.sessions[sessionId] : undefined;

  if (event.type === "safety.alerted" && sessionId) {
    const previous = next.alerts[sessionId];
    const alertId = typeof data.alert_id === "string" ? data.alert_id : previous?.id ?? `session:${sessionId}`;
    next.alerts = {
      ...next.alerts,
      [sessionId]: {
        id: alertId,
        sessionId,
        deviceId: event.device_id ?? previous?.deviceId ?? "",
        status: "ALERT",
        reason: (data.reason as AlertReason | undefined) ?? previous?.reason ?? null,
        incidentType: typeof data.incident_type === "string" ? data.incident_type : previous?.incidentType ?? null,
        raisedAt: previous?.raisedAt ?? event.occurred_at,
        resolvedAt: null,
        resolutionAction: null,
      },
    };
  }

  if ((event.type === "alert.resolved" || event.type === "safety.alert_resolved") && sessionId) {
    const previous = next.alerts[sessionId];
    const alertId = typeof data.alert_id === "string" ? data.alert_id : previous?.id ?? `session:${sessionId}`;
    next.alerts = {
      ...next.alerts,
      [sessionId]: {
        id: alertId,
        sessionId,
        deviceId: event.device_id ?? previous?.deviceId ?? "",
        status: "RESOLVED",
        reason: previous?.reason ?? null,
        incidentType: previous?.incidentType ?? null,
        raisedAt: previous?.raisedAt ?? null,
        resolvedAt: typeof data.resolved_at === "string" ? data.resolved_at : event.occurred_at,
        resolutionAction: typeof data.resolution_action === "string" ? data.resolution_action : null,
      },
    };
  }

  if (event.type === "session.cancelled" && sessionId) {
    const previous = next.alerts[sessionId];
    if (previous || data.from_state === "ALERT") {
      next.alerts = {
        ...next.alerts,
        [sessionId]: {
          id: previous?.id ?? (typeof data.alert_id === "string" ? data.alert_id : `session:${sessionId}`),
          sessionId,
          deviceId: event.device_id ?? previous?.deviceId ?? "",
          status: "RESOLVED",
          reason: previous?.reason ?? null,
          incidentType: previous?.incidentType ?? null,
          raisedAt: previous?.raisedAt ?? null,
          resolvedAt: event.occurred_at,
          resolutionAction: previous?.resolutionAction ?? null,
        },
      };
    }
  }

  // Informational events: logged above, no session field changes.
  if (
    event.type === "alert.notification_requested" ||
    event.type === "contact.notification" ||
    event.type === "technical.power" ||
    event.type === "session.already_active"
  ) {
    return next;
  }

  if (!current) {
    // A session we do not hold (e.g. a new activation): fetch the full object.
    if (sessionId) next.resyncNeeded = true;
    return next;
  }

  const updated: SessionView = { ...current, updated_at: event.occurred_at };
  if (isState(data.state)) updated.state = data.state;
  if (typeof data.version === "number") updated.version = Math.max(updated.version, data.version);

  switch (event.type) {
    case "safety.check_in_requested":
      if (typeof data.checkin_id === "string") updated.checkin_id = data.checkin_id;
      updated.checkin_attempt = typeof data.attempt === "number" ? data.attempt : updated.checkin_attempt;
      updated.response_deadline = typeof data.response_deadline === "string" ? data.response_deadline : null;
      updated.checkin_status = "REQUESTED";
      updated.checkin_requested_at = event.occurred_at;
      updated.next_checkin_at = null;
      updated.retry_at = null;
      break;
    case "safety.check_in_delivered":
      updated.checkin_status = "DELIVERED";
      updated.checkin_delivered_at = event.occurred_at;
      break;
    case "safety.check_in_undelivered":
      updated.checkin_status = "UNDELIVERED";
      updated.undelivered_attempts += 1;
      break;
    case "safety.check_in_unconfirmed":
      updated.missed_attempts = typeof data.missed_attempts === "number" ? data.missed_attempts : updated.missed_attempts;
      updated.retry_at = typeof data.retry_at === "string" ? data.retry_at : null;
      updated.response_deadline = null;
      break;
    case "safety.check_in_confirmed":
      updated.checkin_attempt = 0;
      updated.missed_attempts = 0;
      updated.last_checkin_id = updated.checkin_id ?? updated.last_checkin_id;
      updated.last_checkin_status = "CONFIRMED";
      updated.last_confirmation_at = event.occurred_at;
      updated.checkin_id = null;
      updated.checkin_status = "CONFIRMED";
      updated.response_deadline = null;
      updated.retry_at = null;
      next.resyncNeeded = true; // next_checkin_at is not in the event
      break;
    case "safety.check_in_deferred":
      next.resyncNeeded = true; // which deadline moved is not in the event
      break;
    case "safety.alerted":
      // The status carries the alert id and, for the dispatcher, the last authorized location.
      next.resyncNeeded = true;
      updated.alert_reason = (data.reason as AlertReason | undefined) ?? updated.alert_reason;
      updated.next_checkin_at = null;
      updated.response_deadline = null;
      updated.retry_at = null;
      break;
    case "session.cancelled":
      updated.next_checkin_at = null;
      updated.response_deadline = null;
      updated.retry_at = null;
      break;
    case "technical.issue_reported":
      if (typeof data.issue === "string" && !updated.active_technical_issues.includes(data.issue)) {
        updated.active_technical_issues = [...updated.active_technical_issues, data.issue].sort();
      }
      break;
    case "technical.issue_resolved":
      if (typeof data.issue === "string") {
        updated.active_technical_issues = updated.active_technical_issues.filter((issue) => issue !== data.issue);
      }
      break;
    case "client.status_changed":
      // Published on every phone heartbeat; it carries every field it changes, so no refetch.
      for (const field of CLIENT_STATUS_FIELDS) {
        if (typeof data[field] === "string") (updated as Record<string, unknown>)[field] = data[field];
      }
      break;
    default:
      next.resyncNeeded = true;
  }

  // withSession keeps raisedAt/resolvedAt from the alert record written above.
  return withSession(next, updated);
}

function applyCommand(state: LiveState, response: CommandResponse): LiveState {
  const incoming = response.session;
  const current = state.sessions[incoming.id];
  if (current && time(incoming.updated_at) < time(current.updated_at)) return state;
  return withSession(state, incoming);
}

export function liveReducer(state: LiveState, action: LiveAction): LiveState {
  switch (action.type) {
    case "snapshot":
      return applySnapshot(state, action.status, action.source ?? "http");
    case "sessionSnapshot":
      if (state.sessions[action.session.id]?.version > action.session.version) return state;
      return withSession({ ...state, snapshotAt: action.receivedAt, resyncNeeded: false }, action.session);
    case "event":
      return applyEvent(state, action.event);
    case "command":
      return applyCommand(state, action.response);
    case "acknowledgeRestart":
      return { ...state, restartDetected: false, interrupted: {} };
  }
}

/* --------------------------------------------------------------------------
   Selectors
   -------------------------------------------------------------------------- */

const PRIORITY: SessionState[] = ["ALERT", "UNCONFIRMED", "CHECK_IN", "ACTIVE"];

/**
 * The session a screen should show: the most urgent unfinished one (same order as the
 * backend's system_state), else the most recently changed finished one, else none.
 * One device can never have two unfinished sessions on the backend, so this never shows
 * two ACTIVE sessions for the same person.
 */
export function primarySession(state: LiveState, deviceId?: string | null): SessionView | null {
  const sessions = Object.values(state.sessions).filter((session) => !deviceId || session.device_id === deviceId);
  const unfinished = sessions.filter((session) => isUnfinished(session.state));
  if (unfinished.length > 0) {
    return [...unfinished].sort(
      (a, b) => PRIORITY.indexOf(a.state) - PRIORITY.indexOf(b.state) || time(b.created_at) - time(a.created_at),
    )[0];
  }
  return [...sessions].sort((a, b) => time(b.updated_at) - time(a.updated_at))[0] ?? null;
}

export function unfinishedCount(state: LiveState): number {
  return Object.values(state.sessions).filter((session) => isUnfinished(session.state)).length;
}

export function sortedAlerts(state: LiveState): AlertRecord[] {
  return Object.values(state.alerts).sort((a, b) => {
    if (a.status !== b.status) return a.status === "ALERT" ? -1 : 1;
    return time(b.resolvedAt ?? b.raisedAt) - time(a.resolvedAt ?? a.raisedAt);
  });
}
