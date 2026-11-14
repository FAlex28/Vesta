import type { SessionState } from "../api/contract";

/*
 * Escalation ladder: where the current session stands between ACTIVE and ALERT.
 * Purely a view of the server's state; it adds no data. Steps up to the current one are
 * filled (the current one in full tone), later steps stay grey. IDLE, CANCELLED and an
 * unknown state leave the whole ladder empty.
 */
const STEPS = ["ACTIVE", "CHECK_IN", "UNCONFIRMED", "ALERT"] as const;

const stepTone: Record<(typeof STEPS)[number], string> = {
  ACTIVE: "safe",
  CHECK_IN: "warning",
  UNCONFIRMED: "warning",
  ALERT: "danger",
};

export function StateLadder({ state, compact = false }: { state: SessionState | null; compact?: boolean }) {
  const current = state ? STEPS.indexOf(state as (typeof STEPS)[number]) : -1;
  return (
    <ol className={`ladder ${compact ? "ladder--compact" : ""}`} aria-label="Treapta de escaladare a sesiunii">
      {STEPS.map((step, index) => {
        const position = current < 0 || index > current ? "ahead" : index === current ? "current" : "passed";
        return (
          <li
            key={step}
            className={`ladder-step ladder-step--${position} tone--${stepTone[step]}`}
            aria-current={position === "current" ? "step" : undefined}
          >
            <span className="ladder-bar" aria-hidden="true" />
            <span className="ladder-label num">{step}</span>
          </li>
        );
      })}
    </ol>
  );
}
