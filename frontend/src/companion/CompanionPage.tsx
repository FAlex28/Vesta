import { Headphones, MapPin, Mic, ShieldOff, Siren, Wifi } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { GPS_STALE_AFTER_MS } from "../api/config";
import { api, newEventId } from "../api/client";
import { isUnfinished } from "../api/contract";
import { BrandLockup } from "../components/BrandLockup";
import { LinkBadge } from "../components/LinkBadge";
import { StatusOrb } from "../components/StatusOrb";
import { useNow } from "../services/session";
import { formatAge, formatClock, formatDuration, issueLabel, sessionDuration, stateCopy } from "../state/presentation";
import { useLiveConnection, type LiveLink } from "../state/useLiveConnection";
import { usePageVisibility } from "../state/usePageVisibility";
import { useAssistant, type AssistantPhase } from "./useAssistant";
import { useCompanionAccess } from "./useCompanionAccess";
import { useCompanionSession, type ActionState } from "./useCompanionSession";
import { freshnessOf, useGeolocation } from "./useGeolocation";

const phaseCopy: Record<AssistantPhase, string> = {
  off: "Nepregătită",
  arming: "Se pornește microfonul…",
  ready: "Pregătită",
  speaking: "Vesta vorbește în căști",
  listening: "Vesta te ascultă",
  thinking: "Vesta se gândește…",
};

function companionLink(link: LiveLink, paired: boolean, heartbeatFailures: number): LiveLink {
  if (!paired) return link;
  if (heartbeatFailures >= 3) return { ...link, state: "DISCONNECTED" };
  if (heartbeatFailures >= 1 && link.state === "CONNECTED") return { ...link, state: "RECONNECTING" };
  return link;
}

function ActionStatus({ action, sending, confirmed, failed }: { action: ActionState; sending: string; confirmed: (at: number) => string; failed: string }) {
  if (action.status === "idle") return null;
  if (action.status === "sending") return <p className="action-status action-status--sending" role="status">{sending}</p>;
  if (action.status === "confirmed") return <p className="action-status action-status--ok" role="status">{confirmed(action.at)}</p>;
  return (
    <p className="action-status action-status--failed" role="alert">
      <strong>{failed}</strong>{" "}
      {action.error.kind === "network"
        ? "Serverul nu a răspuns."
        : action.error.kind === "conflict"
          ? "Serverul a refuzat cererea pentru starea curentă a sesiunii."
          : `Răspuns de eroare de la server (${action.error.code ?? action.error.status ?? "necunoscut"}).`}
    </p>
  );
}

export default function CompanionPage() {
  const live = useLiveConnection({ scope: "session" });
  const companion = useCompanionSession(live);
  const access = useCompanionAccess(companion);
  const geo = useGeolocation();
  const now = useNow();
  const assistant = useAssistant({
    credential: companion.credential,
    session: companion.session,
    applySession: companion.applySession,
    reportDelivery: companion.reportDelivery,
  });
  const visibility = usePageVisibility(() => {
    live.reconnectNow();
    void companion.beatNow();
    geo.refresh();
  });
  const [confirmStop, setConfirmStop] = useState(false);
  const chatEnd = useRef<HTMLDivElement | null>(null);

  const paired = companion.credential !== null;
  const known = live.state.snapshotAt !== null || access.lastPollOk !== null;
  const session = companion.session;
  const state = session?.state ?? "IDLE";
  const unfinished = session ? isUnfinished(session.state) : false;
  const falseAlarm = session?.state === "CANCELLED" && session.cancellation_reason === "FALSE_ALARM";
  const copy = stateCopy[state];
  const duration = sessionDuration(session, now);
  const link = companionLink(live.link, paired, companion.heartbeat.consecutiveFailures);
  const fix = geo.gps.fix;
  const fresh = freshnessOf(fix, now);
  const understanding = live.state.assistant.understanding;

  // --- The phone's own GPS: a backup for the GPS on the Vesta device, sent from the start ----
  const locationRequest = useRef<{ capturedAt: number; eventId: string } | null>(null);
  const [phoneLocation, setPhoneLocation] = useState<"idle" | "sent" | "device" | "failed">("idle");
  useEffect(() => {
    const credential = companion.credential;
    if (!credential || !unfinished || !fix || fix.source !== "device") return;
    if (Date.now() - fix.timestamp > GPS_STALE_AFTER_MS) return;
    if (locationRequest.current?.capturedAt !== fix.timestamp) {
      locationRequest.current = { capturedAt: fix.timestamp, eventId: newEventId("location") };
    }
    const request = locationRequest.current;
    let active = true;
    void api
      .location(credential.sessionId, credential.token, request.eventId, {
        latitude: fix.lat,
        longitude: fix.lng,
        captured_at: new Date(fix.timestamp).toISOString(),
        accuracy_meters: fix.accuracy,
        source: "android",
      })
      .then((response) => {
        if (!active) return;
        setPhoneLocation(response.accepted_as_latest ? "sent" : response.location.source === "device_gps" ? "device" : "failed");
      })
      .catch(() => active && setPhoneLocation("failed"));
    return () => {
      active = false;
    };
  }, [companion.credential, unfinished, fix]);

  // --- Technical signals for the dispatcher ---------------------------------------------------
  const { reportClientStatus } = companion;
  useEffect(() => {
    if (!paired || !unfinished) return;
    void reportClientStatus({ page_visibility: visibility.hidden ? "HIDDEN" : "VISIBLE" }, { keepalive: visibility.hidden });
  }, [paired, unfinished, visibility.hidden, reportClientStatus]);
  useEffect(() => {
    if (!paired || !unfinished || !assistant.armed) return;
    void reportClientStatus({ audio_status: "CONNECTED" });
  }, [paired, unfinished, assistant.armed, reportClientStatus]);

  useEffect(() => {
    if (!unfinished) setConfirmStop(false);
  }, [unfinished]);
  useEffect(() => {
    chatEnd.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [assistant.lines.length]);

  // Location from the phone's own GPS, started on its own: no button for the person to press.
  const { startWatch } = geo;
  useEffect(() => {
    startWatch();
  }, [startWatch]);

  const prepare = async () => {
    await assistant.arm();
    if (geo.gps.permission !== "granted") geo.startWatch();
  };

  const stopAction = companion.actions.cancel;
  const sosAction = companion.actions.sos;
  const checkInPending = state === "CHECK_IN" || state === "UNCONFIRMED";

  return (
    <div className={`companion companion--${known ? state.toLowerCase() : "unknown"}`}>
      <header className="companion-bar">
        <BrandLockup caption="Telefon" />
        <LinkBadge link={link} label="Server" />
      </header>

      <main className="companion-main">
        <h1 className="sr-only">Vesta pe telefon</h1>

        {!window.isSecureContext && (
          <p className="notice notice--danger" role="alert">
            Pagina nu este securizată. Deschide adresa care începe cu https://.
          </p>
        )}
        {access.problem === "disabled" && (
          <p className="notice notice--warning" role="alert" data-testid="access-problem">
            Telefonul nu se poate conecta la server.
          </p>
        )}
        {access.problem === "network" && (
          <p className="notice notice--warning" role="alert">Serverul nu răspunde. Se reîncearcă.</p>
        )}

        {assistant.soundBlocked && (
          <button type="button" className="notice notice--warning sound-unlock" data-testid="sound-unlock" onClick={() => undefined}>
            <strong>Atinge ecranul</strong> ca să auzi vocea Vesta.
          </button>
        )}

        {/* --- Earbuds ------------------------------------------------------------------- */}
        <section className={`arm-card ${assistant.armed ? "arm-card--on" : ""}`} aria-labelledby="arm-head" data-testid="arm-card">
          <div className="arm-head">
            <span className="arm-icon" aria-hidden="true"><Headphones size={22} /></span>
            <div>
              <h2 id="arm-head">Vesta în căști</h2>
              <p className="arm-phase" role="status" data-testid="assistant-phase" data-phase={assistant.phase}>
                {assistant.phase === "listening" && <span className="listen-dot" aria-hidden="true" />}
                {phaseCopy[assistant.phase]}
              </p>
            </div>
          </div>
          {!assistant.armed ? (
            <>
              <p className="c-text">Conectează căștile, apasă butonul și permite microfonul și locația.</p>
              <button type="button" className="btn btn--primary btn--xl" data-testid="arm" onClick={() => void prepare()} disabled={assistant.phase === "arming"}>
                <Mic size={20} aria-hidden="true" /> Pregătește Vesta
              </button>
              {assistant.armError && <p className="notice notice--danger" role="alert">{assistant.armError}</p>}
            </>
          ) : (
            <>
              <p className="c-hint">Lasă pagina deschisă. Restul pornește singur.</p>
              <label className="loud-toggle">
                <input
                  type="checkbox"
                  data-testid="loud-toggle"
                  checked={assistant.loud}
                  onChange={(event) => void assistant.setLoud(event.target.checked)}
                />
                Sunet tare în căști
              </label>
              <button type="button" className="text-button" onClick={() => void assistant.disarm()}>Oprește microfonul</button>
            </>
          )}
          {!understanding && (
            <p className="c-hint" data-testid="no-understanding">Mesajele nu pot fi transcrise acum.</p>
          )}
        </section>

        {/* --- Session ------------------------------------------------------------------- */}
        <section className={`c-state c-hero tone--${known ? (falseAlarm ? "neutral" : copy.tone) : "neutral"}`} aria-labelledby="c-state-head" aria-live="polite">
          <div className="c-hero-top">
            <StatusOrb state={known ? state : null} size="md" />
            <div className="c-hero-text">
              <p className="c-eyebrow">{paired ? "Sesiunea ta" : "Vesta"}</p>
              <h2 id="c-state-head" className="c-state-title" data-testid="c-state" data-state={known ? state : "UNKNOWN"}>
                {!known ? "Se așteaptă serverul" : falseAlarm ? "Alarmă falsă: Vesta s-a oprit" : copy.title}
              </h2>
              {known && <p className="c-state-code num">{falseAlarm ? "FALSE_ALARM" : state}</p>}
            </div>
          </div>
          <p className="c-state-detail">
            {!known
              ? "Se conectează…"
              : falseAlarm
                ? "Nimeni nu a fost anunțat."
                : !unfinished
                  ? "Ține butonul apăsat 5 secunde ca să pornești Vesta."
                  : assistant.takenOver
                    ? "Un operator de la dispecerat vorbește cu tine."
                    : state === "ALERT"
                      ? "Dispeceratul și persoana ta de încredere au fost anunțați."
                      : copy.detail}
          </p>
          {session && unfinished && (
            <dl className="c-facts">
              <div><dt>Durată</dt><dd className="num">{duration !== null ? formatDuration(duration) : "—"}</dd></div>
              <div>
                <dt>Înregistrare</dt>
                <dd className="num" data-testid="recording-status">
                  {!assistant.armed
                    ? "oprită"
                    : assistant.recording.failed
                      ? "nu se trimite"
                      : <><span className="rec-dot" aria-hidden="true" /> {assistant.recording.chunks} s</>}
                </dd>
              </div>
            </dl>
          )}
          {session && unfinished && session.active_technical_issues.length > 0 && (
            <ul className="issue-list" aria-label="Avertizări tehnice">
              {session.active_technical_issues.map((code) => <li key={code}>{issueLabel(code)}</li>)}
            </ul>
          )}
        </section>

        {/* --- Conversation -------------------------------------------------------------- */}
        {(assistant.lines.length > 0 || (unfinished && checkInPending)) && (
          <section className="c-section chat-card" aria-labelledby="chat-head" data-testid="chat">
            <h2 id="chat-head" className="c-head">Conversație</h2>
            <div className="chat" role="log" aria-live="polite">
              {assistant.lines.map((line) => (
                <div key={line.id} className={`chat-line chat-line--${line.role}`}>
                  <span className="chat-who">{line.role === "assistant" ? "Vesta" : line.role === "operator" ? "Dispecer" : "Tu"}</span>
                  <p>{line.text}</p>
                  <time className="num">{formatClock(line.at)}</time>
                </div>
              ))}
              <div ref={chatEnd} />
            </div>
            {checkInPending && session?.response_deadline && (
              <p className="c-hint">Dacă nu răspunzi, se anunță dispeceratul.</p>
            )}
          </section>
        )}

        {/* --- Actions ------------------------------------------------------------------- */}
        {paired && unfinished && (
          <section className="c-section c-actions" aria-labelledby="actions-head">
            <h2 id="actions-head" className="sr-only">Acțiuni</h2>
            <button
              type="button"
              className={`btn btn--sos btn--xl btn--sos-orb ${state === "ALERT" ? "btn--sos-orb-sent" : ""}`}
              data-testid="sos"
              onClick={() => void companion.sos()}
              disabled={state === "ALERT" || sosAction.status === "sending"}
            >
              <Siren aria-hidden="true" className="sos-icon" />
              <span className="sos-text">
                {sosAction.status === "sending" ? "Se trimite…" : state === "ALERT" ? "Alertă trimisă" : "Am nevoie de ajutor"}
              </span>
            </button>
            <ActionStatus
              action={sosAction}
              sending="Se anunță dispeceratul…"
              confirmed={(at) => `Dispeceratul a fost anunțat la ${formatClock(at)}.`}
              failed="Alerta nu a plecat."
            />

            <div className="stop-block">
              {confirmStop ? (
                <div className="confirm-row" role="group" aria-label="Confirmă alarma falsă">
                  <p>
                    {state === "ALERT"
                      ? "Oprești alerta? A fost o alarmă falsă?"
                      : "A fost o greșeală? Vesta se oprește."}
                  </p>
                  <button
                    type="button"
                    className="btn btn--outline"
                    data-testid="false-alarm-confirm"
                    onClick={() => void companion.cancel("FALSE_ALARM")}
                    disabled={stopAction.status === "sending"}
                  >
                    Da, alarmă falsă
                  </button>
                  <button type="button" className="btn btn--ghost" onClick={() => setConfirmStop(false)}>Nu</button>
                </div>
              ) : (
                <button type="button" className="btn btn--outline btn--false-alarm" data-testid="false-alarm" onClick={() => setConfirmStop(true)}>
                  <ShieldOff size={18} aria-hidden="true" /> Alarmă falsă: oprește Vesta
                </button>
              )}
            </div>
          </section>
        )}
        {paired && (
          <ActionStatus
            action={stopAction}
            sending="Se oprește Vesta…"
            confirmed={(at) => `Vesta s-a oprit la ${formatClock(at)}.`}
            failed="Vesta nu s-a oprit."
          />
        )}

        {/* --- Location ------------------------------------------------------------------ */}
        <section className="c-section" aria-labelledby="gps-head">
          <h2 id="gps-head" className="c-head">
            <MapPin size={18} aria-hidden="true" /> Locație
            <span className={`fresh fresh--${fresh.toLowerCase()}`} data-testid="gps-freshness">{fresh}</span>
          </h2>
          <p className="c-text">Se trimite automat în timpul sesiunii.</p>
          {fix && fix.source === "device" && (
            <p className="c-hint num" data-testid="phone-location">
              {fix.lat.toFixed(5)}, {fix.lng.toFixed(5)}
              {fix.accuracy != null && ` · ± ${Math.round(fix.accuracy)} m`} · {formatAge(now - fix.timestamp)}
              {unfinished && phoneLocation === "sent" && " · trimisă"}
            </p>
          )}
          {geo.gps.error && <p className="notice notice--warning" role="status">{geo.gps.error}</p>}
          {fix?.source === "device" && fix.accuracy != null && fix.accuracy > 100 && (
            <p className="notice notice--warning" role="status" data-testid="phone-location-warning">
              Precizie slabă. Activează „Locație precisă” pentru Chrome în setările Android.
            </p>
          )}
          {geo.gps.permission === "denied" && (
            <button type="button" className="btn btn--ghost" onClick={geo.startWatch}>
              Încearcă din nou locația
            </button>
          )}
        </section>

        {/* --- Connection ---------------------------------------------------------------- */}
        <section className="c-section" aria-labelledby="conn-head">
          <h2 id="conn-head" className="c-head"><Wifi size={18} aria-hidden="true" /> Conexiune</h2>
          <dl className="c-facts">
            <div><dt>Server</dt><dd data-testid="conn-stream">{link.state}</dd></div>
            <div>
              <dt>Ultimul semnal</dt>
              <dd className="num">
                {paired
                  ? companion.heartbeat.lastOkAt
                    ? formatAge(now - companion.heartbeat.lastOkAt)
                    : "niciunul"
                  : access.lastPollOk
                    ? formatAge(now - access.lastPollOk)
                    : "—"}
              </dd>
            </div>
            <div><dt>Pagina</dt><dd>{visibility.hidden ? "În fundal" : "Deschisă"}</dd></div>
          </dl>
        </section>
      </main>
    </div>
  );
}
