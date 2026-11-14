import { useCallback, useEffect, useRef, useState } from "react";
import { clearGrant, loadGrant, saveGrant, type AccessGrant } from "../api/accessStore";
import { api, ApiError } from "../api/client";
import { COMPANION_POLL_MS } from "../api/config";
import { isUnfinished, type SessionView } from "../api/contract";
import type { CompanionSession } from "./useCompanionSession";

/*
 * Pairing the phone with the person's own Vesta device, once:
 *
 *   label / QR on the device -> https://<vesta>/companion#pair=CODE (or the code typed by hand)
 *   phone /companion         -> device-scoped companion grant (stored on the phone, revocable)
 *
 * The code is read from the URL fragment, which never reaches the server or its logs, and is
 * removed from the address bar immediately. The dispatcher never hands out codes.
 *
 * From then on the phone asks GET /api/v1/companion/session every few seconds. When the
 * joystick has been held for 5 s and the server confirms a session, the answer carries that
 * session and a fresh control token, and the phone takes it over on its own. The phone never
 * starts a session itself.
 */

export type ClaimProblem = "invalid_code" | "too_many_attempts" | "network" | "too_many_phones" | "expired" | null;

/** A short, human label for this phone in the list of paired phones. */
function phoneLabel(): string {
  const agent = navigator.userAgent;
  const model = /Android [\d.]+; ([^;)]+)/.exec(agent)?.[1]?.replace(/ Build.*$/, "").trim();
  if (model && model !== "K") return model.slice(0, 40);
  if (/Android/.test(agent)) return "Telefon Android";
  if (/iPhone/.test(agent)) return "iPhone";
  return "Browser";
}

/** Reads and removes `#pair=CODE` from the address bar. */
function takeFragmentCode(key: string): string | null {
  const match = new RegExp(`(?:^#|&)${key}=([A-Za-z0-9-]+)`).exec(window.location.hash);
  if (!match) return null;
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  return match[1];
}

export { takeFragmentCode };

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
      const claimed = await api.pairDevice(code, phoneLabel());
      const next: AccessGrant = { scope: "companion", deviceId: claimed.device_id, token: claimed.access_token, expiresAt: claimed.expires_at };
      saveGrant(next);
      setGrant(next);
      return true;
    } catch (error) {
      const failure = error instanceof ApiError ? error : null;
      setProblem(
        failure?.status === 429
          ? "too_many_attempts"
          : failure?.code === "TOO_MANY_PHONES"
            ? "too_many_phones"
            : failure?.kind === "unauthorized" || failure?.kind === "invalid"
              ? "invalid_code"
              : "network",
      );
      return false;
    } finally {
      setClaiming(false);
    }
  }, []);

  // QR code on the device: /companion#pair=CODE pairs on open (or when the fragment changes).
  useEffect(() => {
    const fromFragment = () => {
      const code = takeFragmentCode("pair");
      if (code) void claimCode(code);
    };
    fromFragment();
    window.addEventListener("hashchange", fromFragment);
    return () => window.removeEventListener("hashchange", fromFragment);
  }, [claimCode]);

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
