import { lazy, Suspense, useEffect, useState, type FormEvent } from "react";
import { configuredDeviceId } from "./api/config";
import { AlertList } from "./components/AlertList";
import { BrandLockup } from "./components/BrandLockup";
import { EventLog } from "./components/EventLog";
import { LinkBadge } from "./components/LinkBadge";
import { PairingPanel } from "./components/PairingPanel";
import { SectionHead } from "./components/SectionHead";
import { SourceTag } from "./components/SourceTag";
import { StateLadder } from "./components/StateLadder";
import { WatchChain } from "./components/WatchChain";
import { TEST_POINT_LABEL, TIMISOARA_TEST_POINT } from "./data/dashboardData";
import { useNow } from "./services/session";
import { primarySession, sortedAlerts, unfinishedCount } from "./state/liveState";
import {
  formatAge,
  formatClock,
  formatDuration,
  gpsSignal,
  issueLabel,
  issuesOfKind,
  phoneSignal,
  powerSignal,
  sessionDuration,
  stateCopy,
  voiceSignal,
  type Signal,
} from "./state/presentation";
import { useLiveConnection } from "./state/useLiveConnection";
import { usePageVisibility } from "./state/usePageVisibility";

const CityMap = lazy(() => import("./components/CityMap").then((module) => ({ default: module.CityMap })));

// Same order as the sections on the page, so the highlighted item follows scrolling.
const navItems = [
  { id: "session", label: "Stare" },
  { id: "alerts", label: "Alerte" },
  { id: "location", label: "Locație" },
  { id: "events", label: "Jurnal" },
];

function useActiveSection(ids: string[]) {
  const [active, setActive] = useState(ids[0]);

  useEffect(() => {
    const sections = ids
      .map((id) => document.getElementById(id))
      .filter((element): element is HTMLElement => Boolean(element));
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries.filter((entry) => entry.isIntersecting);
        if (visible.length > 0) setActive(visible[0].target.id);
      },
      { rootMargin: "-30% 0px -60% 0px" },
    );
    sections.forEach((section) => observer.observe(section));
    return () => observer.disconnect();
  }, [ids]);

  return [active, setActive] as const;
}

const navIds = navItems.map((item) => item.id);

function SignalCell({ name, signal, testId }: { name: string; signal: Signal; testId: string }) {
  return (
    <div className={`signal signal--${signal.mark} tone--${signal.tone}`} data-testid={testId}>
      <dt>{name}</dt>
      <dd>
        <span className="signal-state">
          <span className="signal-mark" aria-hidden="true" />
          {signal.state}
        </span>
        <span className="signal-detail">{signal.detail}</span>
      </dd>
    </div>
  );
}

function App() {
  const live = useLiveConnection({ scope: "dashboard" });
  const { state, link } = live;
  const now = useNow();
  const [activeSection, setActiveSection] = useActiveSection(navIds);
  const [dashboardKeyInput, setDashboardKeyInput] = useState("");
  usePageVisibility(live.reconnectNow);

  const session = primarySession(state, configuredDeviceId);
  const known = state.snapshotAt !== null;
  const sessionState = session?.state ?? "IDLE";
  const copy = stateCopy[sessionState];
  const tone = known ? copy.tone : "neutral";
  const stale = known && link.state !== "CONNECTED";
  const alerts = sortedAlerts(state);
  const activeAlerts = alerts.filter((alert) => alert.status === "ALERT").length;
  const duration = sessionDuration(session, now);
  const lastConfirmed = session ? session.last_confirmation_at ?? state.lastConfirmedAt[session.id] : undefined;
  // Location facts attached to the most recent alert (status and age only, never coordinates).
  const notice = session ? state.alertNotices[session.id] : undefined;
  const otherIssues = issuesOfKind(session, "other");
  const phone = phoneSignal(session, now);
  const today = new Date(now).toLocaleDateString("ro-RO", { weekday: "long", day: "numeric", month: "long" });
  const [lat, lng] = TIMISOARA_TEST_POINT;

  let deadline: { label: string; value: string } = { label: "Următoarea verificare", value: "—" };
  if (session?.response_deadline) deadline = { label: "Termen de răspuns", value: formatClock(session.response_deadline) };
  else if (session?.retry_at) deadline = { label: "Verificarea se repetă la", value: formatClock(session.retry_at) };
  else if (session?.next_checkin_at) deadline = { label: "Următoarea verificare", value: formatClock(session.next_checkin_at) };

  return (
    <div className="app">
      <header className="masthead">
        <div className="masthead-inner">
          <BrandLockup />
          <nav className="top-nav" aria-label="Secțiuni">
            {navItems.map((item) => (
              <a
                key={item.id}
                href={`#${item.id}`}
                aria-current={activeSection === item.id ? "true" : undefined}
                onClick={() => setActiveSection(item.id)}
              >
                {item.label}
                {item.id === "alerts" && activeAlerts > 0 && <span className="nav-count num">{activeAlerts}</span>}
              </a>
            ))}
          </nav>
          <LinkBadge link={link} />
        </div>
      </header>

      <main className="page">
        <h1 className="sr-only">Panou de siguranță Vesta</h1>

        {live.authRequired && (
          <form
            className="notice notice--warning auth-form"
            onSubmit={(event: FormEvent) => {
              event.preventDefault();
              live.authenticateDashboard(dashboardKeyInput);
              setDashboardKeyInput("");
            }}
          >
            <label htmlFor="dashboard-key"><strong>Autentifică panoul</strong></label>
            <p>Introdu cheia de acces emisă pentru acest dashboard. Cheia rămâne doar în memoria filei.</p>
            <input
              id="dashboard-key"
              type="password"
              autoComplete="current-password"
              value={dashboardKeyInput}
              onChange={(event) => setDashboardKeyInput(event.target.value)}
            />
            <button type="submit" className="btn btn--primary" disabled={!dashboardKeyInput.trim()}>Conectează</button>
          </form>
        )}

        <div className="provenance" role="note">
          <p>
            <span className="src-mark" aria-hidden="true" />
            <span>
              <strong>Starea sesiunii, alertele și jurnalul vin de la serverul Vesta.</strong> Harta arată un punct de
              referință simulat, hașurat. Locația reală nu este afișată în acest panou.
            </span>
          </p>
          <span className="provenance-date">{today}</span>
        </div>

        <section className={`watch tone--${tone}`} id="session" aria-labelledby="session-head">
          <div className="watch-head" aria-live="polite">
            <p className="watch-eyebrow">
              {session ? (
                <>
                  Sesiunea de pe <span className="num">{session.device_id}</span>
                  {unfinishedCount(state) > 1 && " · mai multe sesiuni deschise pe server"}
                </>
              ) : (
                "Sistem"
              )}
            </p>
            <h2 id="session-head" className="watch-title" data-testid="session-title" data-state={known ? sessionState : "UNKNOWN"}>
              <span className="tone-mark" aria-hidden="true" />
              <span className="watch-title-text">{known ? copy.title : "Se așteaptă serverul"}</span>
              {known && <span className="state-code num">{sessionState}</span>}
            </h2>
            <p className="watch-detail">
              {known
                ? copy.detail
                : "Panoul nu afișează nicio stare până nu răspunde serverul Vesta. Nu presupunem că sistemul este pregătit."}
            </p>
          </div>

          {stale && (
            <p className="stale-banner" role="alert" data-testid="stale-banner">
              <strong>Ultima stare cunoscută, primită la {formatClock(state.snapshotAt)}.</strong> Fluxul de la server este{" "}
              {link.state === "DISCONNECTED" ? "întrerupt" : "în reconectare"}; starea reală poate fi diferită.
            </p>
          )}
          {state.restartDetected && (
            <p className="stale-banner stale-banner--info" role="status" data-testid="restart-banner">
              <strong>Serverul a repornit.</strong>{" "}
              {Object.keys(state.interrupted).length > 0
                ? "Sesiunea care rula a fost întreruptă și nu mai există pe server. Pentru o sesiune nouă, ține joystickul apăsat 5 secunde."
                : "Starea afișată a fost reîncărcată de la server."}{" "}
              <button type="button" className="text-button" onClick={live.acknowledgeRestart}>
                Am înțeles
              </button>
            </p>
          )}

          <div className="watch-grid">
            <div className="watch-main">
              <StateLadder state={known ? sessionState : null} />

              <dl className="watch-facts">
                <div>
                  <dt>Durata sesiunii</dt>
                  <dd className="num">{duration !== null ? formatDuration(duration) : "—"}</dd>
                </div>
                <div>
                  <dt>Ultima confirmare de siguranță</dt>
                  <dd className="num">{lastConfirmed ? formatClock(lastConfirmed) : "—"}</dd>
                </div>
                <div>
                  <dt>{deadline.label}</dt>
                  <dd className="num">{deadline.value}</dd>
                </div>
                <div>
                  <dt>Verificări fără răspuns</dt>
                  <dd className="num">{session ? session.missed_attempts : "—"}</dd>
                </div>
              </dl>
            </div>

            {/* The data path beside the person's state. */}
            <div className="watch-system">
              <WatchChain link={link} serverReached={known} phone={phone} />
            </div>
          </div>

          <dl className="signals">
            <SignalCell name="Conexiune telefon" signal={phone} testId="signal-phone" />
            <SignalCell name="Voce · ElevenLabs" signal={voiceSignal(session, state.voiceMode)} testId="signal-voice" />
            <SignalCell name="GPS pe telefon" signal={gpsSignal(session)} testId="signal-gps" />
            <SignalCell name="Alimentare Raspberry Pi" signal={powerSignal(session, state.systemPower)} testId="signal-power" />
          </dl>
          {otherIssues.length > 0 && (
            <ul className="issue-list" aria-label="Alte avertizări tehnice">
              {otherIssues.map((code) => (
                <li key={code}>{issueLabel(code)}</li>
              ))}
            </ul>
          )}

        </section>

        {/* Two independent columns: a short alert list never leaves a hole beside the map. */}
        <div className="board">
          <div className="board-col board-col--main">
          <section className="panel area-alerts" id="alerts" aria-labelledby="alerts-head">
            <SectionHead title="Alerte" id="alerts-head">
              <span className="panel-meta">{activeAlerts === 0 ? "Nicio alertă activă" : activeAlerts === 1 ? "1 alertă activă" : `${activeAlerts} alerte active`}</span>
            </SectionHead>
            <AlertList
              alerts={alerts}
              emptyText={known ? "Nicio alertă de la ultima pornire a serverului." : "Se așteaptă serverul."}
            />
            <p className="panel-note">
              Anularea unei alerte deja emise nu o șterge: rămâne în listă ca RESOLVED, cu ora închiderii. Vesta nu apelează
              112.
            </p>
          </section>

          <section className="panel area-events" id="events" aria-labelledby="events-head">
            <SectionHead title="Jurnal" id="events-head">
              <span className="panel-meta">
                {link.lastMessageAt ? `Ultimul mesaj ${formatAge(now - link.lastMessageAt)}` : "Fără mesaje încă"}
              </span>
            </SectionHead>
            <EventLog log={state.log} />
          </section>
          </div>

          <div className="board-col board-col--side">
          <section className="panel area-map" id="location" aria-labelledby="location-head">
            <SectionHead title="Locație" id="location-head">
              <SourceTag source="simulated" label="Simulat" />
            </SectionHead>
            <div className="map-frame">
              <p className="map-tag">
                <span className="src-mark" aria-hidden="true" />
                Punct de referință simulat, nu este locația unei persoane
              </p>
              <Suspense fallback={<div className="map-loading">Se încarcă harta…</div>}>
                <CityMap point={TIMISOARA_TEST_POINT} state={known ? sessionState : null} kind="simulated" />
              </Suspense>
            </div>
            <dl className="loc-facts">
              <div><dt>Loc</dt><dd>{TEST_POINT_LABEL}</dd></div>
              <div>
                <dt>Coordonate</dt>
                <dd><span className="num sample">{lat.toFixed(4)}, {lng.toFixed(4)}</span></dd>
              </div>
              <div>
                <dt>Locația trimisă cu alerta</dt>
                <dd data-testid="alert-location">
                  {!notice
                    ? "Nu este afișată în acest panou"
                    : notice.locationAvailable
                      ? `Atașată alertei · captată la ${formatClock(notice.capturedAt)}${notice.isStale ? " · veche" : ""}`
                      : "Nicio locație autorizată la emiterea alertei"}
                </dd>
              </div>
            </dl>
          </section>

          <PairingPanel dashboardKey={live.dashboardKey} onUnauthorized={live.requireDashboardAuth} />
          </div>
        </div>
      </main>
    </div>
  );
}

export default App;
