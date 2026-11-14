import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { STREAM_SILENCE_MS, wsUrl } from "../api/config";
import type { CommandResponse, EventEnvelope } from "../api/contract";
import { initialLiveState, liveReducer, type LiveState } from "./liveState";

/*
 * Live server link: the dashboard reads system status; the phone reads its token-scoped
 * session. When the backend has a dashboard key (required outside development/test), the first
 * WebSocket message must be {type:"auth"} with the dashboard key or the session's control token.
 *
 * CONNECTED     the socket is open, the snapshot arrived and the server spoke recently
 *               (events or system.heartbeat). navigator.onLine is never enough on its own.
 * RECONNECTING  connecting for the first time, or retrying after a loss.
 * DISCONNECTED  several attempts failed; retries continue in the background.
 *
 * Each (re)connection takes the server's snapshot and a fresh GET /status, so state after a
 * reconnect or a backend restart always comes from the server, never from local history.
 */

export type LinkState = "CONNECTED" | "RECONNECTING" | "DISCONNECTED";

export type LiveLink = {
  state: LinkState;
  everConnected: boolean;
  lastMessageAt: number | null;
  failedAttempts: number;
};

export type SessionStreamCredential = { sessionId: string; token: string };
export type LiveConnectionOptions = { scope?: "dashboard" | "session" };

const BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 15_000];
const DISCONNECTED_AFTER_FAILURES = 3;

export function useLiveConnection(options: LiveConnectionOptions = {}) {
  const scope = options.scope ?? "dashboard";
  const [state, dispatch] = useReducer(liveReducer, initialLiveState);
  const [dashboardKey, setDashboardKey] = useState("");
  const [sessionCredential, setSessionCredential] = useState<SessionStreamCredential | null>(null);
  const [authRequired, setAuthRequired] = useState(false);
  const [link, setLink] = useState<LiveLink>({
    state: "RECONNECTING",
    everConnected: false,
    lastMessageAt: null,
    failedAttempts: 0,
  });

  const socketRef = useRef<WebSocket | null>(null);
  const retryTimer = useRef<number | undefined>(undefined);
  const silenceTimer = useRef<number | undefined>(undefined);
  const failures = useRef(0);
  const stopped = useRef(false);
  const statusInFlight = useRef<Promise<void> | null>(null);

  const refreshStatus = useCallback(() => {
    if (scope === "session" && !sessionCredential && !import.meta.env.DEV) return Promise.resolve();
    if (statusInFlight.current) return statusInFlight.current;
    const run = (scope === "session" && sessionCredential
      ? api.session(sessionCredential.sessionId, sessionCredential.token).then((session) => {
          dispatch({ type: "sessionSnapshot", session, receivedAt: new Date().toISOString() });
          setAuthRequired(false);
        })
      : api.status(dashboardKey || undefined).then((status) => {
          dispatch({ type: "snapshot", status });
          setAuthRequired(false);
        }))
      .catch((error: unknown) => {
        if (error instanceof ApiError && error.kind === "unauthorized") setAuthRequired(true);
        // The link state already reflects the socket; a failed refresh is retried on the next trigger.
      })
      .finally(() => {
        statusInFlight.current = null;
      });
    statusInFlight.current = run;
    return run;
  }, [scope, sessionCredential, dashboardKey]);

  const connect = useCallback(() => {
    if (stopped.current) return;
    // Before pairing, a production phone page has nothing it may read.
    if (!import.meta.env.DEV && scope === "session" && !sessionCredential) return;
    // Whether the server needs a key is the server's decision (VESTA_DASHBOARD_API_KEY), not the
    // build mode's: connect, send any credential we hold, and treat close code 1008 as "auth needed".
    window.clearTimeout(retryTimer.current);
    const existing = socketRef.current;
    if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) return;

    let socket: WebSocket;
    try {
      socket = new WebSocket(wsUrl());
    } catch {
      scheduleRetry();
      return;
    }
    socketRef.current = socket;
    let snapshotSeen = false;

    const armSilence = () => {
      window.clearTimeout(silenceTimer.current);
      silenceTimer.current = window.setTimeout(() => {
        // No event or heartbeat for too long: the socket may be half-open (phone lost signal).
        if (socketRef.current === socket) socket.close();
      }, STREAM_SILENCE_MS);
    };

    socket.onopen = () => {
      // The credential travels inside the socket, never in the URL. A server running without a
      // dashboard key ignores this message and streams straight away.
      if (scope === "session" && sessionCredential) {
        socket.send(JSON.stringify({ type: "auth", session_id: sessionCredential.sessionId, control_token: sessionCredential.token }));
      } else if (scope === "dashboard" && dashboardKey) {
        socket.send(JSON.stringify({ type: "auth", dashboard_key: dashboardKey }));
      }
      armSilence();
      void refreshStatus();
    };

    socket.onmessage = (message) => {
      let event: EventEnvelope;
      try {
        event = JSON.parse(String(message.data)) as EventEnvelope;
      } catch {
        return;
      }
      armSilence();
      if (event.type === "system.snapshot") snapshotSeen = true;
      dispatch({ type: "event", event });
      if (snapshotSeen) {
        setAuthRequired(false);
        failures.current = 0;
        setLink({ state: "CONNECTED", everConnected: true, lastMessageAt: Date.now(), failedAttempts: 0 });
      }
    };

    socket.onclose = (event) => {
      window.clearTimeout(silenceTimer.current);
      if (socketRef.current === socket) socketRef.current = null;
      // 1008: missing/invalid credential or disallowed Origin. Retrying with the same credential
      // cannot succeed; a new credential re-runs the connection effect.
      if (event.code === 1008) {
        setAuthRequired(true);
        setLink((previous) => ({ ...previous, state: "DISCONNECTED" }));
        return;
      }
      if (!stopped.current) scheduleRetry();
    };

    socket.onerror = () => {
      // onclose follows and schedules the retry.
    };
  }, [scope, sessionCredential, dashboardKey, refreshStatus]);

  function scheduleRetry() {
    failures.current += 1;
    const attempt = failures.current;
    setLink((previous) => ({
      ...previous,
      state: attempt >= DISCONNECTED_AFTER_FAILURES ? "DISCONNECTED" : "RECONNECTING",
      failedAttempts: attempt,
    }));
    window.clearTimeout(retryTimer.current);
    retryTimer.current = window.setTimeout(connect, BACKOFF_MS[Math.min(attempt - 1, BACKOFF_MS.length - 1)]);
  }

  /** Reconnect now (e.g. page visible again) instead of waiting for the backoff. */
  const reconnectNow = useCallback(() => {
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      void refreshStatus();
      return;
    }
    if (socket && socket.readyState === WebSocket.CONNECTING) return;
    setLink((previous) => (previous.state === "CONNECTED" ? { ...previous, state: "RECONNECTING" } : previous));
    connect();
  }, [connect, refreshStatus]);

  useEffect(() => {
    stopped.current = false;
    connect();
    const onOffline = () => {
      // A hint only: mark the link as uncertain and let the socket prove itself again.
      setLink((previous) => (previous.state === "CONNECTED" ? { ...previous, state: "RECONNECTING" } : previous));
      socketRef.current?.close();
    };
    const onOnline = () => reconnectNow();
    window.addEventListener("offline", onOffline);
    window.addEventListener("online", onOnline);
    return () => {
      stopped.current = true;
      window.removeEventListener("offline", onOffline);
      window.removeEventListener("online", onOnline);
      window.clearTimeout(retryTimer.current);
      window.clearTimeout(silenceTimer.current);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [connect, reconnectNow]);

  // Events that could not be applied exactly ask for an authoritative refresh.
  useEffect(() => {
    if (!state.resyncNeeded) return;
    const timer = window.setTimeout(() => void refreshStatus(), 250);
    return () => window.clearTimeout(timer);
  }, [state.resyncNeeded, state.lastSequence, refreshStatus]);

  const applyCommand = useCallback((response: CommandResponse) => dispatch({ type: "command", response }), []);
  const acknowledgeRestart = useCallback(() => dispatch({ type: "acknowledgeRestart" }), []);
  const authenticateDashboard = useCallback((key: string) => {
    setDashboardKey(key.trim());
    setAuthRequired(false);
  }, []);
  const requireDashboardAuth = useCallback(() => setAuthRequired(true), []);
  const authorizeSessionStream = useCallback((credential: SessionStreamCredential | null) => {
    setSessionCredential(credential);
    setAuthRequired(false);
  }, []);

  return {
    state,
    link,
    refreshStatus,
    reconnectNow,
    applyCommand,
    acknowledgeRestart,
    authRequired,
    authenticateDashboard,
    authorizeSessionStream,
    requireDashboardAuth,
    /** Kept in memory only; needed for operator calls such as creating a pairing code. */
    dashboardKey,
  } as const;
}

export type LiveConnection = ReturnType<typeof useLiveConnection>;
export type { LiveState };
