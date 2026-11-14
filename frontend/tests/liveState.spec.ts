import { expect, test } from "playwright/test";
import type { EventEnvelope, SystemStatus } from "../src/api/contract";
import { initialLiveState, liveReducer, primarySession, sortedAlerts, type LiveState } from "../src/state/liveState";
import { makeSession, SESSION_ID } from "./mockBackend";

// Pure reducer tests: no browser needed (run in the "desktop" project only).

const at = (offsetMs: number) => new Date(Date.UTC(2026, 10, 14, 10, 0, 0) + offsetMs).toISOString();

function status(overrides: Partial<SystemStatus> = {}): SystemStatus {
  return { system_state: "ACTIVE", generated_at: at(0), latest_sequence: 1, sessions: [makeSession({ updated_at: at(0) })], voice_mode: "elevenlabs", ...overrides };
}

function event(sequence: number, type: string, data: Record<string, unknown>, eventId = `srv-${sequence}`, offset = sequence * 1000): EventEnvelope {
  return { event_id: eventId, sequence, type, occurred_at: at(offset), session_id: SESSION_ID, device_id: "vesta-01", data };
}

const run = (state: LiveState, ...events: EventEnvelope[]) => events.reduce((current, item) => liveReducer(current, { type: "event", event: item }), state);

test("a repeated activation event (same event_id or old sequence) is applied once", () => {
  const base = liveReducer(initialLiveState, { type: "snapshot", status: status({ latest_sequence: 1 }) });
  const checkIn = event(2, "safety.check_in_requested", { from_state: "ACTIVE", state: "CHECK_IN", attempt: 1 });
  const state = run(base, checkIn, checkIn, { ...checkIn, event_id: "other-id" }, event(1, "session.activated", { state: "ACTIVE" }));
  expect(state.log.filter((entry) => entry.type !== "client.gap")).toHaveLength(1);
  expect(Object.keys(state.sessions)).toHaveLength(1);
  expect(state.sessions[SESSION_ID].state).toBe("CHECK_IN");
});

test("an event for an unknown session asks for a resync instead of inventing a session", () => {
  const base = liveReducer(initialLiveState, { type: "snapshot", status: status({ sessions: [], latest_sequence: 0, system_state: "IDLE" }) });
  const state = run(base, event(1, "session.activated", { from_state: "IDLE", state: "ACTIVE" }));
  expect(Object.keys(state.sessions)).toHaveLength(0);
  expect(state.resyncNeeded).toBe(true);
});

test("duplicate alert events keep one alert; cancellation resolves it without deleting it", () => {
  const base = liveReducer(initialLiveState, { type: "snapshot", status: status() });
  const alerted = event(2, "safety.alerted", { from_state: "ACTIVE", state: "ALERT", reason: "explicit_sos" });
  let state = run(base, alerted, alerted, { ...alerted, event_id: "srv-dup", sequence: 3 });
  expect(sortedAlerts(state)).toHaveLength(1);
  expect(sortedAlerts(state)[0]).toMatchObject({ status: "ALERT", reason: "explicit_sos", raisedAt: at(2000) });

  state = run(state, event(4, "session.cancelled", { from_state: "ALERT", state: "CANCELLED" }));
  const alerts = sortedAlerts(state);
  expect(alerts).toHaveLength(1);
  expect(alerts[0]).toMatchObject({ status: "RESOLVED", resolvedAt: at(4000), raisedAt: at(2000) });
});

test("a snapshot after a restart drops vanished sessions and reports them as interrupted", () => {
  let state = liveReducer(initialLiveState, { type: "snapshot", status: status({ latest_sequence: 5 }) });
  state = liveReducer(state, { type: "snapshot", status: status({ generated_at: at(60_000), latest_sequence: 0, sessions: [], system_state: "IDLE" }) });
  expect(state.restartDetected).toBe(true);
  expect(Object.keys(state.sessions)).toHaveLength(0);
  expect(state.interrupted[SESSION_ID]).toBeDefined();
  expect(primarySession(state)).toBeNull();
});

test("an older status answer never overwrites newer state", () => {
  let state = liveReducer(initialLiveState, { type: "snapshot", status: status({ generated_at: at(10_000), sessions: [makeSession({ state: "ALERT", alert_reason: "explicit_sos", updated_at: at(10_000) })] }) });
  state = liveReducer(state, { type: "snapshot", status: status({ generated_at: at(5_000) }) });
  expect(state.sessions[SESSION_ID].state).toBe("ALERT");
});

test("a GET /status that overtakes in-flight stream events neither fakes a gap nor rolls state back", () => {
  let state = liveReducer(initialLiveState, { type: "snapshot", status: status({ latest_sequence: 0 }), source: "stream" });
  const resolved = makeSession({ state: "CANCELLED", alert_reason: "explicit_sos", updated_at: at(3000) });
  state = liveReducer(state, { type: "snapshot", status: status({ generated_at: at(3500), latest_sequence: 3, sessions: [resolved] }) });
  state = run(
    state,
    event(1, "safety.check_in_confirmed", { from_state: "CHECK_IN", state: "ACTIVE" }),
    event(2, "safety.alerted", { from_state: "ACTIVE", state: "ALERT", reason: "explicit_sos" }),
    event(3, "session.cancelled", { from_state: "ALERT", state: "CANCELLED" }),
  );
  expect(state.log.some((entry) => entry.type === "client.gap")).toBe(false);
  expect(state.log).toHaveLength(3);
  expect(state.sessions[SESSION_ID].state).toBe("CANCELLED");
  expect(state.lastConfirmedAt[SESSION_ID]).toBe(at(1000));
  expect(sortedAlerts(state)[0]).toMatchObject({ status: "RESOLVED", raisedAt: at(2000), resolvedAt: at(3000) });
});

test("a reconnected stream whose snapshot skips ahead marks the missed events", () => {
  let state = liveReducer(initialLiveState, { type: "snapshot", status: status({ latest_sequence: 1 }), source: "stream" });
  state = liveReducer(state, { type: "snapshot", status: status({ generated_at: at(9000), latest_sequence: 4 }), source: "stream" });
  const gap = state.log.find((entry) => entry.type === "client.gap");
  expect(gap && "missed" in gap ? gap.missed : 0).toBe(3);
});

test("a sequence gap is marked in the log and triggers a resync", () => {
  const base = liveReducer(initialLiveState, { type: "snapshot", status: status({ latest_sequence: 1 }), source: "stream" });
  const state = run(base, event(5, "technical.issue_reported", { from_state: "ACTIVE", state: "ACTIVE", issue: "audio_interrupted" }));
  expect(state.resyncNeeded).toBe(true);
  expect(state.log.some((entry) => entry.type === "client.gap")).toBe(true);
  expect(state.sessions[SESSION_ID].active_technical_issues).toEqual(["audio_interrupted"]);
});

test("one device never shows two ACTIVE sessions: the unfinished one wins over older finished ones", () => {
  const old = makeSession({ id: "old", state: "CANCELLED", updated_at: at(-1000), created_at: at(-9000) });
  const current = makeSession({ id: "current", state: "ACTIVE", updated_at: at(-2000), created_at: at(-3000) });
  const state = liveReducer(initialLiveState, { type: "snapshot", status: status({ sessions: [old, current] }) });
  expect(primarySession(state, "vesta-01")?.id).toBe("current");
});
