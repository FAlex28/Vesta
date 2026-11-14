import { AlertList } from "../components/AlertList";
import { BrandLockup } from "../components/BrandLockup";
import { LinkBadge } from "../components/LinkBadge";
import { sortedAlerts } from "../state/liveState";
import { formatClock } from "../state/presentation";
import { useLiveConnection } from "../state/useLiveConnection";
import { usePageVisibility } from "../state/usePageVisibility";

/*
 * Trusted-contact view, for controlled tests. It reflects alerts from the server stream:
 * ALERT while active, RESOLVED with the closing time after cancellation; a sent alert is
 * never removed. It shows no coordinates and sends no SMS, calls or push notifications.
 * Real contact access needs an agreed authorization (MISSING_CONTRACT.contactAccess).
 */
export default function ContactPage() {
  const live = useLiveConnection();
  usePageVisibility(live.reconnectNow);
  const known = live.state.snapshotAt !== null;
  const alerts = sortedAlerts(live.state);
  const active = alerts.filter((alert) => alert.status === "ALERT");

  return (
    <div className="companion contact">
      <header className="companion-bar">
        <BrandLockup caption="Contact de încredere" />
        <LinkBadge link={live.link} label="Server" />
      </header>
      <main className="companion-main">
        <h1 className="sr-only">Alerte Vesta pentru contactul de încredere</h1>
        <p className="notice" role="note">
          Această pagină nu trimite SMS, apeluri sau notificări și nu apelează 112.
        </p>
        {live.authRequired && (
          <p className="notice notice--warning" role="alert">
            Accesul securizat al contactului nu este configurat pentru acest server. Alertele nu pot fi afișate aici.
          </p>
        )}

        <section className={`c-state tone--${active.length > 0 ? "danger" : "neutral"}`} aria-live="assertive">
          <p className="c-eyebrow">Situația curentă</p>
          <h2 className="c-state-title" data-testid="contact-state">
            <span className="tone-mark" aria-hidden="true" />
            {!known ? "Se așteaptă serverul" : active.length > 0 ? "Alertă activă" : "Nicio alertă activă"}
          </h2>
          <p className="c-state-detail">
            {!known
              ? "Pagina afișează alertele doar după ce răspunde serverul."
              : active.length > 0
                ? `${active[0].raisedAt ? `Vesta a emis o alertă la ${formatClock(active[0].raisedAt)}.` : "Vesta a emis o alertă."} Încearcă să contactezi persoana direct.`
                : "Alertele închise rămân mai jos, cu ora închiderii."}
          </p>
          {known && live.link.state !== "CONNECTED" && (
            <p className="notice notice--warning" role="alert">
              Legătura cu serverul este întreruptă. Lista arată ultima stare primită ({formatClock(live.state.snapshotAt)}).
            </p>
          )}
        </section>

        <section className="c-section" aria-labelledby="contact-alerts">
          <h2 id="contact-alerts" className="c-head">Alerte</h2>
          <AlertList alerts={alerts} emptyText={known ? "Nicio alertă de la ultima pornire a serverului." : "Se așteaptă serverul."} />
          <p className="c-hint">
            Locația nu este afișată pe această pagină.
          </p>
        </section>
      </main>
    </div>
  );
}
