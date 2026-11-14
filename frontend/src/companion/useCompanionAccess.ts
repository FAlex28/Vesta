import { useCallback, useEffect, useRef, useState } from "react";
import { clearGrant, loadGrant, saveGrant, type AccessGrant } from "../api/accessStore";
import { api, ApiError } from "../api/client";
import { COMPANION_POLL_MS } from "../api/config";
import { isUnfinished, type SessionView } from "../api/contract";
import type { CompanionSession } from "./useCompanionSession";

/*
 * Pairing the phone with the Vesta device, once:
 *
 *   dashboard (operator) -> "Cod pentru telefon" -> 8-character one-time code
 *   phone /companion     -> types the code -> device-scoped companion grant (stored, 24 h)
 *
 * From then on the phone asks GET /api/v1/companion/session every few seconds. When the
 * joystick has been held for 5 s and the server confirms a session, the answer carries that
 * session and a fresh control token, and the phone takes it over on its own. The phone never
 * starts a session itself.
 */

export type ClaimProblem = "invalid_code" | "too_many_attempts" | "network" | "wrong_kind" | "expired" | null;

export function useCompanionAccess(companion: CompanionSession) {
  const [grant, setGrant] = useState<AccessGrant | null>(() => loadGrant("companion"));
  const [claiming, setClaiming] = useState(false);
  const [problem, setProblem] = useState<ClaimProblem>(null);
  const [lastPollOk, setLastPollOk] = useState<number | null>(null);
  const [waiting, setWaiting] = useState(false);
  const adopting = useRef<string | null>(null);
  const { adopt, credential, session } = companion;

  const claimCode = useCallback(async (rawCode: string): Promise<boolean> => {
    const code = rawCode.trim();
    if (!code) return false;
    setClaiming(true);
    setProblem(null);
    try {
      const claimed = await api.claimPairingCode(code);
      if (claimed.scope !== "companion") {
        setProblem("wrong_kind");
        return false;
      }
      const next: AccessGrant = { scope: "companion", deviceId: claimed.device_id, token: claimed.access_token, expiresAt: claimed.expires_at };
      saveGrant(next);
      setGrant(next);
      return true;
    } catch (error) {
      const failure = error instanceof ApiError ? error : null;
      setProblem(
        failure?.status === 429 ? "too_many_attempts" : failure?.kind === "unauthorized" || failure?.kind === "invalid" ? "invalid_code" : "network",
      );
      return false;
    } finally {
      setClaiming(false);
    }
  }, []);

  const forget = useCallback(() => {
    clearGrant("companion");
    setGrant(null);
    setWaiting(false);
  }, []);

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
        if (!target || !answer.control_token || !isUnfinished(target.state)) {
          setWaiting(true);
          return;
        }
        setWaiting(false);
        if (adopting.current === target.id || credential?.sessionId === target.id) return;
        adopting.current = target.id;
        await adopt(answer.control_token, target);
        adopting.current = null;
      } catch (error) {
        if (stopped) return;
        if (error instanceof ApiError && error.kind === "unauthorized") {
          clearGrant("companion");
          setGrant(null);
          setProblem("expired");
        }
      }
    };
    void poll();
    const timer = window.setInterval(poll, COMPANION_POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [grant, controlling, adopt, credential?.sessionId]);

  return { grant, claimCode, claiming, problem, forget, waiting, lastPollOk } as const;
}
