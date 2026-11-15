import { useCallback, useEffect, useRef, useState } from "react";
import { clearGrant, loadGrant, saveGrant, type AccessGrant } from "../api/accessStore";
import { api, ApiError } from "../api/client";
import { COMPANION_POLL_MS } from "../api/config";
import { isUnfinished, type SessionView } from "../api/contract";
import type { CompanionSession } from "./useCompanionSession";

/*
 * The person's phone, without any pairing code (server in VESTA_OPEN_ACCESS mode):
 *
 *   phone /companion -> POST /api/v1/access/open {scope: companion} -> device-scoped grant
 *
 * The grant stays on the phone. From then on the phone asks GET /api/v1/companion/session every
 * few seconds; when the joystick has been held for 5 s and the server confirms a session, the
 * answer carries the session and a fresh control token and the phone takes it over on its own.
 */

export type AccessProblem = "disabled" | "network" | null;

/** Reads and removes `#key=VALUE` from the address bar (kept for old links). */
export function takeFragmentCode(key: string): string | null {
  const match = new RegExp(`(?:^#|&)${key}=([A-Za-z0-9-]+)`).exec(window.location.hash);
  if (!match) return null;
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  return match[1];
}

const RETRY_MS = 5_000;

// One request per scope at a time (React runs effects twice in development).
const inflight: Partial<Record<"companion" | "contact", Promise<AccessGrant>>> = {};
function requestGrant(scope: "companion" | "contact"): Promise<AccessGrant> {
  inflight[scope] ??= api
    .openAccess(scope)
    .then((opened): AccessGrant => {
      const next: AccessGrant = { scope, deviceId: opened.device_id, token: opened.access_token, expiresAt: opened.expires_at };
      saveGrant(next);
      return next;
    })
    .finally(() => {
      delete inflight[scope];
    });
  return inflight[scope]!;
}

export function useOpenGrant(scope: "companion" | "contact") {
  const [grant, setGrant] = useState<AccessGrant | null>(() => loadGrant(scope));
  const [problem, setProblem] = useState<AccessProblem>(null);

  useEffect(() => {
    if (grant) return;
    let stopped = false;
    let timer = 0;
    const acquire = async () => {
      try {
        const next = await requestGrant(scope);
        if (stopped) return;
        setProblem(null);
        setGrant(next);
      } catch (error) {
        if (stopped) return;
        setProblem(error instanceof ApiError && error.kind === "not_found" ? "disabled" : "network");
        timer = window.setTimeout(acquire, RETRY_MS);
      }
    };
    void acquire();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [grant, scope]);

  /** The server no longer knows this grant (e.g. its state was reset): take a new one. */
  const renew = useCallback(() => {
    clearGrant(scope);
    setGrant(null);
  }, [scope]);

  return { grant, problem, renew } as const;
}

export function useCompanionAccess(companion: CompanionSession) {
  const { grant, problem, renew } = useOpenGrant("companion");
  const [lastPollOk, setLastPollOk] = useState<number | null>(null);
  const adopting = useRef<string | null>(null);
  const { adopt, credential, session } = companion;

  // A running session we already control needs no polling; otherwise ask the server.
  const controlling = Boolean(credential && session && isUnfinished(session.state));
  useEffect(() => {
    if (!grant || controlling) return;
    let stopped = false;
    const poll = async () => {
      try {
        const answer = await api.companionSession(grant.token);
        if (stopped) return;
        setLastPollOk(Date.now());
        const target: SessionView | null = answer.session;
        if (!target || !answer.control_token || !isUnfinished(target.state)) return;
        if (adopting.current === target.id || credential?.sessionId === target.id) return;
        adopting.current = target.id;
        await adopt(answer.control_token, target);
        adopting.current = null;
      } catch (error) {
        if (stopped) return;
        if (error instanceof ApiError && error.kind === "unauthorized") renew();
      }
    };
    void poll();
    const timer = window.setInterval(poll, COMPANION_POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [grant, controlling, adopt, credential?.sessionId, renew]);

  return { grant, problem, lastPollOk } as const;
}
