import type { AlertRecord } from "../state/liveState";
import { alertReasonCopy, formatClock } from "../state/presentation";

/*
 * Active and resolved alerts, one row per alert (keyed by session id; the contract has no
 * alert_id). A cancelled alert is never removed: it becomes RESOLVED with its closing time.
 */
export function AlertList({ alerts, emptyText }: { alerts: AlertRecord[]; emptyText: string }) {
  if (alerts.length === 0) {
    return <p className="empty-note">{emptyText}</p>;
  }
  return (
    <ul className="alerts" aria-label="Alerte">
      {alerts.map((alert) => (
        <li key={alert.sessionId} className={`alert-row alert-row--${alert.status.toLowerCase()}`} data-testid="alert-row">
          <span className="alert-status">
            <span className="alert-mark" aria-hidden="true" />
            <strong>{alert.status === "ALERT" ? "ALERT" : "RESOLVED"}</strong>
            <span className="alert-status-text">{alert.status === "ALERT" ? "alertă activă" : "alertă închisă"}</span>
          </span>
          <span className="alert-reason">{alert.reason ? alertReasonCopy[alert.reason] : "Motiv neraportat"}</span>
          <dl className="alert-times">
            <div>
              <dt>Emisă</dt>
              <dd className={alert.raisedAt ? "num" : "alert-unknown"}>
                {alert.raisedAt ? formatClock(alert.raisedAt) : "înainte de deschiderea paginii"}
              </dd>
            </div>
            <div>
              <dt>Închisă</dt>
              <dd className="num">{alert.resolvedAt ? formatClock(alert.resolvedAt) : "—"}</dd>
            </div>
          </dl>
        </li>
      ))}
    </ul>
  );
}
