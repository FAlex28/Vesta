import type { LiveState } from "../state/liveState";
import { alertReasonCopy, eventCopy, formatClock, issueLabel } from "../state/presentation";
import type { AlertReason } from "../api/contract";

// Readable names for the `source` field of server events.
const sourceCopy: Record<string, string> = {
  android: "telefon",
  android_heartbeat: "telefon",
  voice_agent: "asistentul vocal",
  voice_gateway: "serviciul vocal",
  dashboard: "panou",
  backend: "server",
};

function detailFor(type: string, data: Record<string, unknown>): string {
  const parts: string[] = [];
  if (typeof data.from_state === "string" && typeof data.state === "string" && data.from_state !== data.state) {
    parts.push(`${data.from_state} → ${data.state}`);
  }
  if (typeof data.issue === "string") parts.push(issueLabel(data.issue));
  if (typeof data.attempt === "number") parts.push(`încercarea ${data.attempt}`);
  if (typeof data.reason === "string" && type === "safety.alerted") {
    parts.push(alertReasonCopy[data.reason as AlertReason] ?? data.reason);
  }
  if (typeof data.source === "string") parts.push(`prin ${sourceCopy[data.source] ?? "sistem"}`);
  return parts.join(" · ");
}

/** Events received from the server stream since this page opened, deduplicated by event_id. */
export function EventLog({ log, limit = 40 }: { log: LiveState["log"]; limit?: number }) {
  const rows = log.slice(0, limit);
  return (
    <ol className="timeline" aria-label="Evenimente">
      {rows.map((entry) =>
        entry.type === "client.gap" && "missed" in entry ? (
          <li className="tl-row tl-row--gap" key={entry.id}>
            <time className="num">{formatClock(entry.occurredAt).slice(0, 5)}</time>
            <span className="tl-node" aria-hidden="true" />
            <span className="tl-copy">
              <strong>Evenimente neprimite</strong>
              <span>
                {entry.missed === 1 ? "Un eveniment" : `${entry.missed} evenimente`} din timpul deconectării nu apar aici. Starea a
                fost reîncărcată de la server.
              </span>
            </span>
          </li>
        ) : (
          "sequence" in entry && (
            <li className={`tl-row tl-row--${eventCopy[entry.type]?.kind ?? "system"}`} key={entry.id} data-testid="log-row">
              <time className="num" title={formatClock(entry.occurredAt)}>
                {formatClock(entry.occurredAt).slice(0, 5)}
              </time>
              <span className="tl-node" aria-hidden="true" />
              <span className="tl-copy">
                <strong>{eventCopy[entry.type]?.title ?? entry.type}</strong>
                {detailFor(entry.type, entry.data) && <span>{detailFor(entry.type, entry.data)}</span>}
              </span>
            </li>
          )
        ),
      )}
      <li className="tl-row tl-row--pending">
        <span className="tl-time-empty num" aria-hidden="true">--:--</span>
        <span className="tl-node" aria-hidden="true" />
        <span className="tl-copy">
          <span>
            {rows.length === 0
              ? "Niciun eveniment primit de la deschiderea paginii. Evenimentele noi apar aici în timp real."
              : "Jurnalul arată evenimentele primite de la deschiderea paginii."}
          </span>
        </span>
      </li>
    </ol>
  );
}
