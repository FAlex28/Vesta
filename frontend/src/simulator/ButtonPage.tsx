import { ExternalLink, Hand } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, newEventId } from "../api/client";
import type { SessionState, SystemStatus } from "../api/contract";
import { BrandLockup } from "../components/BrandLockup";
import { StatusOrb } from "../components/StatusOrb";
import { formatClock, stateCopy } from "../state/presentation";

/*
 * Button simulator: stands in for the joystick on the Raspberry Pi (GPIO17) while the Pi has no
 * battery. Hold for 5 s, exactly like the joystick: releasing earlier does nothing. The server
 * then starts the same session the joystick would (POST /api/v1/simulator/press, only in
 * VESTA_OPEN_ACCESS mode), and the phone, dispatcher and contact react as in the real flow.
 */

const HOLD_MS = 5000;

type Result =
  | { kind: "idle" }
  | { kind: "early"; heldMs: number }
  | { kind: "sending" }
  | { kind: "ok"; outcome: string; at: number }
  | { kind: "error"; text: string };

function useServerState() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [reachable, setReachable] = useState<boolean | null>(null);
  useEffect(() => {
    let stop = false;
    const load = () =>
      api
        .status()
        .then((answer) => {
          if (stop) return;
          setStatus(answer);
          setReachable(true);
        })
        .catch(() => !stop && setReachable(false));
    void load();
    const timer = window.setInterval(load, 2000);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, []);
  return { status, reachable, refresh: () => void api.status().then(setStatus).catch(() => undefined) } as const;
}

export default function ButtonPage() {
  const [progress, setProgress] = useState(0);
  const [holding, setHolding] = useState(false);
  const [result, setResult] = useState<Result>({ kind: "idle" });
  const startedAt = useRef<number | null>(null);
  const frame = useRef<number>(0);
  const fired = useRef(false);
  const server = useServerState();

  const live = server.status?.sessions.filter((session) => session.state !== "CANCELLED") ?? [];
  const state: SessionState = server.status ? (live[0]?.state ?? "IDLE") : "IDLE";
  const copy = stateCopy[state];

  const fire = useCallback(async () => {
    setResult({ kind: "sending" });
    try {
      navigator.vibrate?.([80, 60, 80]);
    } catch {
      // optional
    }
    try {
      const answer = await api.simulatorPress(newEventId("buton"));
      setResult({ kind: "ok", outcome: answer.outcome, at: Date.now() });
      server.refresh();
    } catch (error) {
      setResult({
        kind: "error",
        text:
          error instanceof ApiError && error.kind === "not_found"
            ? "Butonul nu este disponibil pe acest server."
            : "Serverul nu răspunde.",
      });
    }
  }, [server]);

  const tick = useCallback(() => {
    if (startedAt.current === null) return;
    const elapsed = performance.now() - startedAt.current;
    setProgress(Math.min(1, elapsed / HOLD_MS));
    if (elapsed >= HOLD_MS && !fired.current) {
      fired.current = true;
      void fire();
      return;
    }
    frame.current = requestAnimationFrame(tick);
  }, [fire]);

  const press = useCallback(() => {
    if (startedAt.current !== null || result.kind === "sending") return;
    fired.current = false;
    startedAt.current = performance.now();
    setHolding(true);
    setResult({ kind: "idle" });
    frame.current = requestAnimationFrame(tick);
  }, [tick, result.kind]);

  const release = useCallback(() => {
    if (startedAt.current === null) return;
    const held = performance.now() - startedAt.current;
    cancelAnimationFrame(frame.current);
    startedAt.current = null;
    setHolding(false);
    setProgress(0);
    if (!fired.current) setResult({ kind: "early", heldMs: held });
  }, []);

  useEffect(() => () => cancelAnimationFrame(frame.current), []);

  const seconds = Math.ceil((1 - progress) * (HOLD_MS / 1000));

  return (
    <div className={`companion sim companion--${state.toLowerCase()}`}>
      <header className="companion-bar">
        <BrandLockup caption="Simulator buton" />
      </header>

      <main className="companion-main sim-main">
        <h1 className="sr-only">Simulatorul butonului Vesta</h1>

        <section className="sim-stage" aria-label="Butonul Vesta">
          <button
            type="button"
            className={`sim-button ${holding ? "sim-button--holding" : ""} ${result.kind === "ok" ? "sim-button--done" : ""}`}
            style={{ "--progress": `${progress * 360}deg` } as React.CSSProperties}
            data-testid="sim-button"
            aria-label="Ține apăsat 5 secunde pentru a porni Vesta"
            onPointerDown={(event) => {
              event.currentTarget.setPointerCapture(event.pointerId);
              press();
            }}
            onPointerUp={release}
            onPointerCancel={release}
            onLostPointerCapture={release}
            onKeyDown={(event) => {
              if ((event.key === " " || event.key === "Enter") && !event.repeat) {
                event.preventDefault();
                press();
              }
            }}
            onKeyUp={(event) => {
              if (event.key === " " || event.key === "Enter") release();
            }}
            onContextMenu={(event) => event.preventDefault()}
          >
            <span className="sim-face">
              <Hand size={40} aria-hidden="true" />
              <span className="sim-count num">{holding ? `${seconds} s` : "5 s"}</span>
              <span className="sim-label">{holding ? "Ține apăsat…" : "Ține apăsat"}</span>
            </span>
          </button>

          <p className={`sim-result sim-result--${result.kind}`} role="status" aria-live="polite" data-testid="sim-result">
            {result.kind === "idle" && "Ține apăsat 5 secunde."}
            {result.kind === "early" && `Eliberat după ${(result.heldMs / 1000).toFixed(1)} s: prea devreme.`}
            {result.kind === "sending" && "Se pornește…"}
            {result.kind === "ok" &&
              (result.outcome === "ACTIVATED"
                ? `Vesta a pornit la ${formatClock(result.at)}.`
                : "Există deja o sesiune pornită.")}
            {result.kind === "error" && result.text}
          </p>
        </section>

        <section className={`c-state c-hero tone--${server.status ? copy.tone : "neutral"}`} aria-labelledby="sim-state">
          <div className="c-hero-top">
            <StatusOrb state={server.status ? state : null} size="md" />
            <div className="c-hero-text">
              <p className="c-eyebrow">Stare</p>
              <h2 id="sim-state" className="c-state-title" data-testid="sim-state" data-state={state}>
                {server.reachable === false ? "Serverul nu răspunde" : server.status ? copy.title : "Se așteaptă serverul"}
              </h2>
              {server.status && <p className="c-state-code num">{state}</p>}
            </div>
          </div>
        </section>

        <nav className="sim-links" aria-label="Celelalte ecrane">
          <a className="btn btn--ghost" href="/companion" target="_blank" rel="noreferrer">
            Telefonul victimei <ExternalLink size={15} aria-hidden="true" />
          </a>
          <a className="btn btn--ghost" href="/" target="_blank" rel="noreferrer">
            Dispecerat <ExternalLink size={15} aria-hidden="true" />
          </a>
          <a className="btn btn--ghost" href="/contact" target="_blank" rel="noreferrer">
            Persoana de încredere <ExternalLink size={15} aria-hidden="true" />
          </a>
        </nav>
      </main>
    </div>
  );
}
