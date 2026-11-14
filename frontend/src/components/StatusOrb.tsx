import { BellRing, Hourglass, Shield, ShieldCheck, ShieldOff, Siren, TriangleAlert, type LucideIcon } from "lucide-react";
import type { SessionState } from "../api/contract";
import { stateCopy } from "../state/presentation";

/*
 * The session state at a glance: a core icon inside rings in the state colour.
 * The rings breathe only while a session runs (slow for ACTIVE, faster while Vesta waits for
 * an answer, steady red for ALERT). Motion follows the server's state; it never means "live"
 * on its own, and it stops for prefers-reduced-motion.
 */

const icon: Record<SessionState, LucideIcon> = {
  IDLE: Shield,
  ACTIVE: ShieldCheck,
  CHECK_IN: BellRing,
  UNCONFIRMED: TriangleAlert,
  ALERT: Siren,
  CANCELLED: ShieldOff,
};

const motion: Record<SessionState, "still" | "calm" | "urgent" | "alert"> = {
  IDLE: "still",
  ACTIVE: "calm",
  CHECK_IN: "urgent",
  UNCONFIRMED: "urgent",
  ALERT: "alert",
  CANCELLED: "still",
};

export function StatusOrb({ state, size = "lg" }: { state: SessionState | null; size?: "lg" | "md" | "sm" }) {
  const Icon = state ? icon[state] : Hourglass;
  const tone = state ? stateCopy[state].tone : "neutral";
  return (
    <div className={`orb orb--${size} orb--${state ? motion[state] : "still"} tone--${tone}`} aria-hidden="true">
      <span className="orb-ring orb-ring--outer" />
      <span className="orb-ring orb-ring--inner" />
      <span className="orb-core">
        <Icon strokeWidth={2} />
      </span>
    </div>
  );
}
