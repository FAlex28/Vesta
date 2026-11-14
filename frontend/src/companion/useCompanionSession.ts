import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, newEventId } from "../api/client";
import { configuredDeviceId, HEARTBEAT_INTERVAL_MS } from "../api/config";
import { isUnfinished, type CommandResponse, type ContractTechnicalIssue, type SessionView } from "../api/contract";
import type { LiveConnection } from "../state/useLiveConnection";

/*
 * Phone-side session control.
 *
 * Credential: the control token is kept in React memory only. It is never written to
 * localStorage/sessionStorage, a URL or a log, so a reload or a closed tab needs pairing
 * again. The API contract does not define how firmware safely hands its token to the phone
 * (MISSING_CONTRACT.phonePairing); until Agent 2/3
 * agree one, pairing is either unavailable or, for controlled tests only, entered manually.
 *
 * Commands: each user action gets one event_id. A retry of the same action reuses it, so the
 * server applies it at most once. Success is reported only from the server's answer.
 */

export type Credential = { token: string; sessionId: string; deviceId: string };

export type ActionName = "confirm" | "sos" | "cancel";

export type ActionState =
  | { status: "idle" }
  | { status: "sending"; eventId: string; startedAt: number }
  | { status: "confirmed"; eventId: string; at: number; duplicate: boolean }
  | { status: "failed"; eventId: string; at: number; error: ApiError };

export type PairingProblem =
  | "no_session"
  | "several_sessions"
  | "session_id_required"
  | "rejected"
  | "network"
  | "session_gone"
  | "token_invalid"
  | null;

export type HeartbeatHealth = {
  lastOkAt: number | null;
  consecutiveFailures: number;
  lastError: ApiError | null;
};

const idle: ActionState = { status: "idle" };

function asApiError(error: unknown): ApiError {
  return error instanceof ApiError ? error : new ApiError("network", null, null, "Cererea nu a putut fi trimisă.");
}

export function useCompanionSession(live: LiveConnection) {
  const [credential, setCredential] = useState<Credential | null>(null);
  const [pairingProblem, setPairingProblem] = useState<PairingProblem>(null);
  const [pairing, setPairing] = useState(false);
  const [actions, setActions] = useState<Record<ActionName, ActionState>>({ confirm: idle, sos: idle, cancel: idle });
  const [heartbeat, setHeartbeat] = useState<HeartbeatHealth>({ lastOkAt: null, consecutiveFailures: 0, lastError: null });
  const reportedIssues = useRef(new Map<ContractTechnicalIssue, boolean>());
  const { applyCommand, refreshStatus } = live;

  const session: SessionView | null = credential ? live.state.sessions[credential.sessionId] ?? null : null;
  const interrupted = credential ? live.state.interrupted[credential.sessionId] ?? null : null;

  const unpair = useCallback((problem: PairingProblem) => {
    setCredential(null);
    live.authorizeSessionStream(null);
    setPairingProblem(problem);
    setHeartbeat({ lastOkAt: null, consecutiveFailures: 0, lastError: null });
    reportedIssues.current.clear();
  }, [live.authorizeSessionStream]);

  /** Bind a control token to the server's current session for this device. */
  const pair = useCallback(
    async (rawToken: string, rawSessionId?: string): Promise<boolean> => {
      const token = rawToken.trim();
      if (!token) return false;
      const requestedSessionId = rawSessionId?.trim();
      let sessionResolved = false;
      setPairing(true);
      setPairingProblem(null);
      try {
        const candidates = requestedSessionId
          ? [await api.session(requestedSessionId, token)]
          : (await api.status()).sessions.filter(
              (item) => isUnfinished(item.state) && (!configuredDeviceId || item.device_id === configuredDeviceId),
            );
        if (candidates.length === 0) {
          setPairingProblem("no_session");
          return false;
        }
        if (candidates.length > 1) {
          setPairingProblem("several_sessions");
          return false;
        }
        const target = candidates[0];
        sessionResolved = true;
        if (!isUnfinished(target.state) || (configuredDeviceId && target.device_id !== configuredDeviceId)) {
          setPairingProblem("no_session");
          return false;
        }
        // An authenticated call proves that the token belongs to this session.
        const response = await api.heartbeat(target.id, token);
        applyCommand(response);
        live.authorizeSessionStream({ sessionId: target.id, token });
        setCredential({ token, sessionId: target.id, deviceId: target.device_id });
        setHeartbeat({ lastOkAt: Date.now(), consecutiveFailures: 0, lastError: null });
        setActions({ confirm: idle, sos: idle, cancel: idle });
        return true;
      } catch (error) {
        const failure = asApiError(error);
        setPairingProblem(
          failure.kind === "network"
            ? "network"
            : failure.kind === "unauthorized" && !requestedSessionId && !sessionResolved
              ? "session_id_required"
              : "rejected",
        );
        return false;
      } finally {
        setPairing(false);
      }
    },
    [applyCommand, live.authorizeSessionStream],
  );

  const handleAuthFailure = useCallback(
    (error: ApiError) => {
      if (error.kind === "unauthorized") unpair("token_invalid");
      else if (error.kind === "not_found") unpair("session_gone");
    },
    [unpair],
  );

  // Connectivity lease. The backend turns an expired lease into network_disconnected, never ALERT.
  useEffect(() => {
    if (!credential) return;
    const finished = session && !isUnfinished(session.state);
    if (finished) return;
    let cancelled = false;
    const beat = async () => {
      try {
        const response = await api.heartbeat(credential.sessionId, credential.token);
        if (cancelled) return;
        applyCommand(response);
        setHeartbeat({ lastOkAt: Date.now(), consecutiveFailures: 0, lastError: null });
      } catch (error) {
        if (cancelled) return;
        const failure = asApiError(error);
        if (failure.code === "SESSION_CANCELLED") return void refreshStatus();
        setHeartbeat((previous) => ({ ...previous, consecutiveFailures: previous.consecutiveFailures + 1, lastError: failure }));
        handleAuthFailure(failure);
      }
    };
    const timer = window.setInterval(beat, HEARTBEAT_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [credential, session?.state, applyCommand, refreshStatus, handleAuthFailure]);

  /** Send a heartbeat right away, e.g. when the page becomes visible again. */
  const beatNow = useCallback(async () => {
    if (!credential) return;
    try {
      const response = await api.heartbeat(credential.sessionId, credential.token);
      applyCommand(response);
      setHeartbeat({ lastOkAt: Date.now(), consecutiveFailures: 0, lastError: null });
    } catch (error) {
      const failure = asApiError(error);
      if (failure.code === "SESSION_CANCELLED") return void refreshStatus();
      setHeartbeat((previous) => ({ ...previous, consecutiveFailures: previous.consecutiveFailures + 1, lastError: failure }));
      handleAuthFailure(failure);
    }
  }, [credential, applyCommand, refreshStatus, handleAuthFailure]);

  const run = useCallback(
    async (
      name: ActionName,
      call: (sessionId: string, token: string, eventId: string, session: SessionView) => Promise<CommandResponse>,
    ) => {
      if (!credential) return false;
      if (!session || !isUnfinished(session.state)) return false;
      // Reuse the event_id of a failed attempt of the same action: the retry stays idempotent.
      const previous = actions[name];
      if (previous.status === "sending") return false;
      const eventId = previous.status === "failed" ? previous.eventId : newEventId(name);
      setActions((current) => ({ ...current, [name]: { status: "sending", eventId, startedAt: Date.now() } }));
      try {
        const response = await call(credential.sessionId, credential.token, eventId, session);
        applyCommand(response);
        setActions((current) => ({
          ...current,
          [name]: { status: "confirmed", eventId, at: Date.now(), duplicate: response.duplicate },
        }));
        return true;
      } catch (error) {
        const failure = asApiError(error);
        setActions((current) => ({ ...current, [name]: { status: "failed", eventId, at: Date.now(), error: failure } }));
        handleAuthFailure(failure);
        if (failure.kind === "conflict") void refreshStatus();
        return false;
      }
    },
    [credential, session, actions, applyCommand, refreshStatus, handleAuthFailure],
  );

  const confirmSafe = useCallback(
    () => run("confirm", (id, token, eventId, current) => {
      if (!current.checkin_id) {
        throw new ApiError("conflict", 409, "CHECKIN_NOT_PENDING", "Nu există o verificare activă de confirmat.");
      }
      return api.confirmSafe(id, current.checkin_id, token, eventId);
    }),
    [run],
  );
  const sos = useCallback(() => run("sos", api.sos), [run]);
  const cancel = useCallback(
    (reason?: string) => run("cancel", (id, token, eventId) => api.cancel(id, token, eventId, reason)),
    [run],
  );
  const resetAction = useCallback((name: ActionName) => setActions((current) => ({ ...current, [name]: idle })), []);

  /** Report a technical condition once per change; the backend defers check-in escalation meanwhile. */
  const reportIssue = useCallback(
    async (issue: ContractTechnicalIssue, active: boolean) => {
      if (!credential) return;
      if (reportedIssues.current.get(issue) === active) return;
      if (!active && !reportedIssues.current.has(issue)) return;
      reportedIssues.current.set(issue, active);
      try {
        const response = await api.technicalStatus(credential.sessionId, credential.token, newEventId(`tech-${issue}`), issue, active);
        applyCommand(response);
      } catch (error) {
        reportedIssues.current.delete(issue);
        handleAuthFailure(asApiError(error));
      }
    },
    [credential, applyCommand, refreshStatus, handleAuthFailure],
  );

  // A new session (new activation) means the old token no longer controls anything useful.
  useEffect(() => {
    if (credential && interrupted) unpair("session_gone");
  }, [credential, interrupted, unpair]);

  return {
    credential,
    session,
    pairing,
    pairingProblem,
    pair,
    unpair,
    heartbeat,
    beatNow,
    actions,
    confirmSafe,
    sos,
    cancel,
    resetAction,
    reportIssue,
  } as const;
}

export type CompanionSession = ReturnType<typeof useCompanionSession>;
