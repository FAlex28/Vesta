import { linkCopy } from "../state/presentation";
import type { LiveLink } from "../state/useLiveConnection";

/** Server link indicator: CONNECTED / RECONNECTING / DISCONNECTED, with the code for clarity. */
export function LinkBadge({ link, label = "Server" }: { link: LiveLink; label?: string }) {
  const copy = linkCopy[link.state];
  const text = !link.everConnected && link.state === "RECONNECTING" ? "Se conectează" : copy.label;
  return (
    <span className={`link-badge tone--${copy.tone}`} role="status" aria-live="polite" data-testid="link-badge" data-state={link.state}>
      <span className="link-dot" aria-hidden="true" />
      <span className="link-label">{label}:</span> {text}
    </span>
  );
}
