import { Headphones, MapPin, QrCode, Siren, Wifi } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { configuredDeviceId, GPS_STALE_AFTER_MS, manualPairingEnabled } from "../api/config";
import { api, newEventId } from "../api/client";
import { isUnfinished } from "../api/contract";
import { BrandLockup } from "../components/BrandLockup";
import { LinkBadge } from "../components/LinkBadge";
import { SourceTag } from "../components/SourceTag";
import { StateLadder } from "../components/StateLadder";
import { StatusOrb } from "../components/StatusOrb";
import { useNow } from "../services/session";
import { primarySession } from "../state/liveState";
import { formatAge, formatClock, formatDuration, issueLabel, sessionDuration, stateCopy } from "../state/presentation";
import { useLiveConnection, type LiveLink } from "../state/useLiveConnection";
import { usePageVisibility } from "../state/usePageVisibility";
import { EmergencyContacts } from "./EmergencyContacts";
import { useCompanionAccess, type ClaimProblem } from "./useCompanionAccess";
import { useCompanionSession, type ActionState, type PairingProblem } from "./useCompanionSession";
import { freshnessOf, useGeolocation } from "./useGeolocation";
import { useVoice, type VoicePhase } from "./useVoice";

const pairingProblemCopy: Record<Exclude<PairingProblem, null>, string> = {
  no_session: "Nu există o sesiune în desfășurare pe server. Ține joystickul apăsat 5 secunde, apoi încearcă din nou.",
  several_sessions: "Serverul are mai multe sesiuni deschise, iar telefonul nu poate alege automat sesiunea corectă.",
  session_id_required: "Introdu identificatorul sesiunii împreună cu codul de control primit în procesul de asociere.",
  rejected: "Serverul a respins codul pentru sesiunea curentă.",
  network: "Serverul nu a răspuns. Verifică conexiunea și încearcă din nou.",
  session_gone:
    "Sesiunea a fost întreruptă: serverul nu o mai are (de exemplu după o repornire). Conversația vocală anterioară nu mai este activă. Pentru o sesiune nouă, ține joystickul apăsat 5 secunde; un telefon asociat o preia singur.",
  token_invalid:
    "Codul de control nu mai este valid (a expirat sau serverul a repornit). Comenzile nu mai pot fi trimise pentru această sesiune.",
};

const claimProblemCopy: Record<Exclude<ClaimProblem, null>, string> = {
  invalid_code: "Codul nu se potrivește cu dispozitivul. Verifică eticheta Vesta și încearcă din nou.",
  too_many_attempts: "Prea multe coduri greșite. Așteaptă un minut și încearcă din nou.",
  network: "Serverul nu a răspuns. Verifică conexiunea și încearcă din nou.",
  too_many_phones: "Dispozitivul are deja numărul maxim de telefoane. Elimină unul de pe un telefon asociat.",
  expired: "Asocierea acestui telefon a expirat sau a fost eliminată. Scanează din nou codul de pe dispozitiv.",
};

const voicePhaseCopy: Record<VoicePhase, { label: string; tone: "neutral" | "safe" | "warning" | "danger" }> = {
  off: { label: "Conversația nu a pornit", tone: "neutral" },
  requesting_mic: { label: "Se cere permisiunea pentru microfon…", tone: "warning" },
  mic_error: { label: "Eroare microfon", tone: "danger" },
  requesting_access: { label: "Se pregătește conversația…", tone: "warning" },
  connecting: { label: "Se conectează la asistent…", tone: "warning" },
  connected: { label: "Audio conectat", tone: "safe" },
  disconnected: { label: "Audio deconectat", tone: "danger" },
  playback_error: { label: "Eroare redare", tone: "danger" },
  unavailable: { label: "Serviciu vocal indisponibil", tone: "danger" },
  scripted: { label: "Doar text, fără audio", tone: "warning" },
  ended: { label: "Conversație încheiată", tone: "neutral" },
};

function companionLink(link: LiveLink, paired: boolean, heartbeatFailures: number): LiveLink {
  if (!paired) return link;
  if (heartbeatFailures >= 3) return { ...link, state: "DISCONNECTED" };
  if (heartbeatFailures >= 1 && link.state === "CONNECTED") return { ...link, state: "RECONNECTING" };
  return link;
}

function ActionStatus({ action, sending, confirmed, failed }: { action: ActionState; sending: string; confirmed: (at: number) => string; failed: string }) {
  if (action.status === "idle") return null;
  if (action.status === "sending")
    return <p className="action-status action-status--sending" role="status">{sending}</p>;
  if (action.status === "confirmed")
    return <p className="action-status action-status--ok" role="status">{confirmed(action.at)}</p>;
  return (
    <p className="action-status action-status--failed" role="alert">
      <strong>{failed}</strong>{" "}
      {action.error.kind === "network"
        ? "Serverul nu a răspuns."
        : action.error.kind === "conflict"
          ? "Serverul a refuzat cererea pentru starea curentă a sesiunii."
          : action.error.kind === "unauthorized"
            ? "Telefonul nu mai este autorizat pentru această sesiune."
            : `Răspuns de eroare de la server (${action.error.code ?? action.error.status ?? "necunoscut"}).`}
    </p>
  );
}

export default function CompanionPage() {
  const live = useLiveConnection({ scope: "session" });
  const companion = useCompanionSession(live);
  const access = useCompanionAccess(companion);
  const [codeInput, setCodeInput] = useState("");
  const geo = useGeolocation();
  const now = useNow();
  const voiceCtl = useVoice(companion.credential, live.state.voiceMode, {
    confirmSafe: companion.confirmSafe,
    requestHelp: companion.sos,
    reportIssue: companion.reportIssue,
    reportVoiceStatus: companion.reportVoiceStatus,
  });
  const visibility = usePageVisibility(() => {
    live.reconnectNow();
    void companion.beatNow();
    geo.refresh();
  });
  const [tokenInput, setTokenInput] = useState("");
  const [sessionIdInput, setSessionIdInput] = useState("");
  const [confirmEnd, setConfirmEnd] = useState(false);
  const [shareLocation, setShareLocation] = useState(false);
  const [locationUploadState, setLocationUploadState] = useState<"idle" | "sending" | "sent" | "failed">("idle");
  const locationRequest = useRef<{ capturedAt: number; eventId: string } | null>(null);

  const paired = companion.credential !== null;
  const known = live.state.snapshotAt !== null;
  // Paired: the authoritative session for our credential. Otherwise a read-only view of the server.
  const session = paired ? companion.session : primarySession(live.state, configuredDeviceId);
  const state = session?.state ?? "IDLE";
  const copy = stateCopy[state];
  const unfinished = session ? isUnfinished(session.state) : false;
  const checkInPending = state === "CHECK_IN" || state === "UNCONFIRMED";
  const lastConfirmed = session?.last_confirmation_at ?? (session ? live.state.lastConfirmedAt[session.id] : undefined);
  const duration = sessionDuration(session, now);
  const link = companionLink(live.link, paired, companion.heartbeat.consecutiveFailures);
  const fresh = freshnessOf(geo.gps.fix, now);
  const voice = voiceCtl.voice;

  useEffect(() => {
    if (paired && unfinished && shareLocation && geo.gps.fix?.source === "device" && companion.credential) return;
    setShareLocation(false);
  }, [paired, unfinished, shareLocation, geo.gps.fix?.source, companion.credential]);

  useEffect(() => {
    const fix = geo.gps.fix;
    const credential = companion.credential;
    if (!shareLocation || !paired || !unfinished || !fix || fix.source !== "device" || !credential) return;
    if (Date.now() - fix.timestamp > GPS_STALE_AFTER_MS) return;

    if (locationRequest.current?.capturedAt !== fix.timestamp) {
      locationRequest.current = { capturedAt: fix.timestamp, eventId: newEventId("location") };
    }
    const request = locationRequest.current;
    if (!request) return;
    let active = true;
    setLocationUploadState("sending");
    void api.location(credential.sessionId, credential.token, request.eventId, {
      latitude: fix.lat,
      longitude: fix.lng,
      captured_at: new Date(fix.timestamp).toISOString(),
      accuracy_meters: fix.accuracy,
      source: "android",
    }).then((response) => {
      if (active) setLocationUploadState(response.accepted_as_latest ? "sent" : "failed");
    }).catch(() => {
      if (active) setLocationUploadState("failed");
    });
    return () => { active = false; };
  }, [shareLocation, paired, unfinished, geo.gps.fix, companion.credential]);

  // Technical signals for the dashboard: page visibility and whether the phone has a recent GPS
  // fix (never the coordinates; those go only through the explicit sharing switch below).
  const { reportClientStatus } = companion;
  useEffect(() => {
    if (!paired || !unfinished) return;
    void reportClientStatus({ page_visibility: visibility.hidden ? "HIDDEN" : "VISIBLE" }, { keepalive: visibility.hidden });
  }, [paired, unfinished, visibility.hidden, reportClientStatus]);
  const gpsStatus = geo.gps.fix?.source !== "device" ? "GPS_UNAVAILABLE" : fresh === "CURRENT" ? "GPS_AVAILABLE" : "GPS_STALE";
  const gpsInUse = geo.gps.permission === "granted" || geo.gps.fix?.source === "device";
  const gpsReported = useRef(false);
  useEffect(() => {
    if (!paired || !unfinished) {
      gpsReported.current = false;
      return;
    }
    // Until the user turns GPS on, the server's default (GPS_UNAVAILABLE) is already right.
    if (!gpsInUse && !gpsReported.current) return;
    gpsReported.current = true;
    void reportClientStatus({ gps_status: gpsStatus });
  }, [paired, unfinished, gpsInUse, gpsStatus, reportClientStatus]);

  // A finished session has no conversation on the server any more: close ours too.
  const { open: voiceOpen, stop: stopVoice } = voiceCtl;
  useEffect(() => {
    if (paired && known && !unfinished && voiceOpen) void stopVoice();
  }, [paired, known, unfinished, voiceOpen, stopVoice]);

  // The server owns the quick-cancel window; never infer it from check-in counters.
  const quickCancel = Boolean(
    state === "ACTIVE" && session?.quick_cancel_until && Date.parse(session.quick_cancel_until) > now,
  );

  // When the server asks for a check, prompt visually and tell the voice agent.
  const previousState = useRef(state);
  const { notifyCheckIn } = voiceCtl;
  useEffect(() => {
    if (state === "CHECK_IN" && previousState.current !== "CHECK_IN") {
      notifyCheckIn();
      // Bring the question on screen wherever the person had scrolled.
      window.setTimeout(() => document.getElementById("checkin-head")?.scrollIntoView({ behavior: "smooth", block: "center" }), 50);
      try {
        navigator.vibrate?.([200, 100, 200]);
      } catch {
        // Vibration is optional.
      }
    }
    previousState.current = state;
  }, [state, notifyCheckIn]);

  // A confirmed "Sunt bine" belongs to one check; clear it once the server moves on.
  const { resetAction } = companion;
  useEffect(() => {
    if (state === "CHECK_IN") resetAction("confirm");
    if (!unfinished) setConfirmEnd(false);
  }, [state, unfinished, resetAction]);

  const onPair = (event: FormEvent) => {
    event.preventDefault();
    void companion.pair(tokenInput, sessionIdInput).then((pairedNow) => {
      if (pairedNow) {
        setTokenInput("");
        setSessionIdInput("");
      }
    });
  };

  const awayMs = visibility.lastAway ? visibility.lastAway.to - visibility.lastAway.from : 0;
  const showAway = visibility.lastAway !== null && awayMs > 5_000 && now - visibility.lastAway.to < 60_000;
  const sosAction = companion.actions.sos;

  // The check-in reached the user: the prompt is on the open screen, or the voice agent has it.
  const { reportDelivery } = companion;
  const pendingCheckin = state === "CHECK_IN" && session?.checkin_id && session.checkin_status === "REQUESTED" ? session.checkin_id : null;
  useEffect(() => {
    if (!paired || !pendingCheckin) return;
    if (!visibility.hidden) void reportDelivery(pendingCheckin, "android");
    else if (voiceCtl.open) void reportDelivery(pendingCheckin, "voice");
  }, [paired, pendingCheckin, visibility.hidden, voiceCtl.open, reportDelivery]);

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
            <strong>Conexiunea paginii nu este securizată.</strong> Browserul blochează microfonul și locația pe această
            adresă. SOS-ul și „Sunt bine” funcționează în continuare.
          </p>
        )}
        {showAway && (
          <p className="notice" role="status" data-testid="away-notice">
            Pagina a fost în fundal {formatDuration(awayMs)}. Starea a fost cerută din nou de la server. Cât timp pagina
            era ascunsă, microfonul, GPS-ul și conexiunea pot fi fost oprite de browser.
          </p>
        )}
        {link.state !== "CONNECTED" && known && (
          <p className="notice notice--warning" role="alert" data-testid="link-notice">
            <strong>{link.state === "DISCONNECTED" ? "Fără legătură cu serverul." : "Legătura cu serverul se reface."}</strong>{" "}
            Starea de mai jos este ultima primită ({formatClock(live.state.snapshotAt)}). Butoanele trimit cererea, dar
            succesul apare doar după răspunsul serverului.
          </p>
        )}

        {/* --- Session state ------------------------------------------------ */}
        <section className={`c-state c-hero tone--${known ? copy.tone : "neutral"}`} aria-labelledby="c-state-head" aria-live="polite">
          <div className="c-hero-top">
            <StatusOrb state={known ? state : null} size="md" />
            <div className="c-hero-text">
              <p className="c-eyebrow">{paired ? "Sesiunea ta" : "Starea serverului"}</p>
              <h2 id="c-state-head" className="c-state-title" data-testid="c-state" data-state={known ? state : "UNKNOWN"}>
                {known ? copy.title : "Se așteaptă serverul"}
              </h2>
              {known && <p className="c-state-code num">{state}</p>}
            </div>
          </div>
          {known && <StateLadder state={state} compact />}
          <p className="c-state-detail">
            {!known
              ? "Nu afișăm nicio stare până nu răspunde serverul Vesta."
              : state === "IDLE" || state === "CANCELLED"
                ? "Ține joystickul apăsat continuu timp de 5 secunde pentru activarea Vesta. Starea devine ACTIVE doar după confirmarea serverului."
                : copy.detail}
          </p>
          {session && unfinished && (
            <dl className="c-facts">
              <div><dt>Durată</dt><dd className="num">{duration !== null ? formatDuration(duration) : "—"}</dd></div>
              <div><dt>Ultima confirmare</dt><dd className="num">{lastConfirmed ? formatClock(lastConfirmed) : "—"}</dd></div>
            </dl>
          )}
          {session && session.active_technical_issues.length > 0 && (
            <ul className="issue-list" aria-label="Avertizări tehnice">
              {session.active_technical_issues.map((code) => <li key={code}>{issueLabel(code)}</li>)}
            </ul>
          )}
        </section>

        {/* --- Check-in prompt ---------------------------------------------- */}
        {paired && checkInPending && (
          <section className="checkin" role="alert" aria-labelledby="checkin-head" data-testid="checkin-prompt">
            <h2 id="checkin-head">Vesta întreabă dacă ești bine</h2>
            <p>
              {state === "UNCONFIRMED"
                ? "Prima verificare nu a primit răspuns. Confirmă acum, dacă poți."
                : "Răspunde vocal sau apasă butonul de mai jos. Dacă două verificări afișate rămân fără răspuns, Vesta trimite alerta."}
              {session?.response_deadline && <> Termen: <span className="num">{formatClock(session.response_deadline)}</span>.</>}
            </p>
            <button
              type="button"
              className="btn btn--safe btn--xl"
              onClick={() => void companion.confirmSafe()}
              disabled={companion.actions.confirm.status === "sending" || !session?.checkin_id}
            >
              Sunt bine
            </button>
            {!session?.checkin_id && <p role="status">Se așteaptă identificatorul verificării de la server.</p>}
            <ActionStatus
              action={companion.actions.confirm}
              sending="Se trimite confirmarea…"
              confirmed={(at) => `Serverul a primit confirmarea la ${formatClock(at)}.`}
              failed="Confirmarea nu a fost primită de server."
            />
          </section>
        )}

        {/* --- Pairing -------------------------------------------------------- */}
        {!paired && (
          <section className="c-section" aria-labelledby="pair-head">
            <h2 id="pair-head" className="c-head"><QrCode size={18} aria-hidden="true" /> Asocierea telefonului</h2>
            {companion.pairingProblem && (
              <p className="notice notice--warning" role="alert" data-testid="pairing-problem">
                {pairingProblemCopy[companion.pairingProblem]}
              </p>
            )}
            {access.problem && (
              <p className="notice notice--warning" role="alert" data-testid="claim-problem">
                {claimProblemCopy[access.problem]}
              </p>
            )}
            {access.grant ? (
              <div className="pair-waiting" data-testid="pair-waiting">
                <div className="hold-hint" aria-hidden="true">
                  <span className="hold-ring" />
                  <span className="hold-label num">5 s</span>
                </div>
                <p className="c-text">
                  <strong>Telefon asociat cu dispozitivul <span className="num">{access.grant.deviceId}</span>.</strong>{" "}
                  Ține joystickul apăsat continuu 5 secunde. Când serverul confirmă activarea, sesiunea apare aici singură.
                </p>
                <p className="c-hint" role="status">
                  {access.lastPollOk
                    ? `Serverul a răspuns ${formatAge(now - access.lastPollOk)}: nicio sesiune în desfășurare.`
                    : "Se verifică serverul…"}
                </p>
                <button type="button" className="btn btn--ghost" onClick={access.forget}>
                  Uită asocierea
                </button>
              </div>
            ) : (
              <form
                className="pair-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  void access.claimCode(codeInput).then((ok) => ok && setCodeInput(""));
                }}
              >
                <ol className="pair-steps">
                  <li>Deschide camera telefonului și scanează <strong>codul QR de pe eticheta dispozitivului Vesta</strong>.</li>
                  <li>Sau scrie aici codul de 10 caractere tipărit sub QR.</li>
                </ol>
                <label htmlFor="pair-code">Codul de pe dispozitivul Vesta</label>
                <input
                  id="pair-code"
                  type="text"
                  inputMode="text"
                  autoCapitalize="characters"
                  autoComplete="one-time-code"
                  spellCheck={false}
                  maxLength={16}
                  value={codeInput}
                  onChange={(event) => setCodeInput(event.target.value)}
                />
                <button type="submit" className="btn btn--primary" disabled={access.claiming || codeInput.trim().length < 4}>
                  {access.claiming ? "Se verifică…" : "Asociază cu codul"}
                </button>
                <p className="c-hint">
                  Asocierea rămâne pe acest telefon 30 de zile. O poți șterge oricând, de aici sau de pe alt telefon asociat.
                </p>
              </form>
            )}
            <p className="c-text">
              Comenzile (SOS, „Sunt bine”, anulare, conversație) cer ca telefonul să fie autorizat pentru sesiunea curentă.
              Identificatorul sesiunii singur nu este suficient.
            </p>
            {manualPairingEnabled && (
              <form className="pair-form" onSubmit={onPair}>
                <label htmlFor="session-id">Identificatorul sesiunii</label>
                <input
                  id="session-id"
                  type="text"
                  autoComplete="off"
                  spellCheck={false}
                  value={sessionIdInput}
                  onChange={(event) => setSessionIdInput(event.target.value)}
                />
                <label htmlFor="control-token">Cod de control al sesiunii</label>
                <input
                  id="control-token"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={tokenInput}
                  onChange={(event) => setTokenInput(event.target.value)}
                />
                <button type="submit" className="btn btn--primary" disabled={companion.pairing || !tokenInput.trim()}>
                  {companion.pairing ? "Se verifică…" : "Asociază telefonul"}
                </button>
                <p className="c-hint">Codul rămâne doar în memoria acestei pagini. La reîncărcare trebuie introdus din nou.</p>
              </form>
            )}
          </section>
        )}

        {/* --- Actions -------------------------------------------------------- */}
        <section className="c-section c-actions" aria-labelledby="actions-head">
          <h2 id="actions-head" className="sr-only">Acțiuni</h2>

          <button
            type="button"
            className={`btn btn--sos btn--xl btn--sos-orb ${state === "ALERT" ? "btn--sos-orb-sent" : ""}`}
            data-testid="sos"
            onClick={() => void companion.sos()}
            disabled={!paired || !unfinished || state === "ALERT" || sosAction.status === "sending"}
          >
            <Siren aria-hidden="true" className="sos-icon" />
            <span className="sos-text">
              {sosAction.status === "sending"
                ? "Se trimite SOS…"
                : sosAction.status === "failed"
                  ? "Reîncearcă SOS"
                  : state === "ALERT"
                    ? "Alertă activă"
                    : "SOS · Cer ajutor"}
            </span>
          </button>
          <ActionStatus
            action={sosAction}
            sending="Se trimite cererea de ajutor la server…"
            confirmed={(at) => `Serverul a confirmat SOS-ul la ${formatClock(at)}. Sesiunea este în alertă.`}
            failed="Alerta NU a fost confirmată ca transmisă."
          />
          <p className="c-hint">
            {!paired
              ? "SOS devine disponibil după asocierea telefonului cu sesiunea."
              : state === "ALERT"
                ? "Alerta este deja activă pe server."
                : !unfinished
                  ? "SOS funcționează în timpul unei sesiuni Vesta."
                  : "Funcționează și fără conversația vocală. Vesta nu apelează 112."}
          </p>

          {paired && !checkInPending && (
            <button type="button" className="btn btn--safe" disabled title="Disponibil când Vesta cere confirmarea">
              Sunt bine
            </button>
          )}
          {paired && !checkInPending && <p className="c-hint">„Sunt bine” devine activ când serverul cere confirmarea.</p>}
          {/* The prompt closes as soon as the server accepts; its answer stays visible here. */}
          {paired && !checkInPending && state !== "ALERT" && companion.actions.confirm.status === "confirmed" && (
            <ActionStatus
              action={companion.actions.confirm}
              sending=""
              confirmed={(at) => `Serverul a primit confirmarea la ${formatClock(at)}.`}
              failed=""
            />
          )}

          {paired && unfinished && (
            <div className="end-block">
              {quickCancel ? (
                <>
                  <button
                    type="button"
                    className="btn btn--outline"
                    data-testid="quick-cancel"
                    onClick={() => void companion.cancel("ACCIDENTAL_ACTIVATION")}
                    disabled={companion.actions.cancel.status === "sending"}
                  >
                    Anulează activarea
                  </button>
                  <p className="c-hint">
                    Ai pornit Vesta din greșeală? Anularea încheie sesiunea fără alertă. Funcționează doar cu pagina deschisă și
                    telefonul deblocat.
                  </p>
                </>
              ) : confirmEnd ? (
                <div className="confirm-row" role="group" aria-label="Confirmă încheierea">
                  <p>
                    {state === "ALERT"
                      ? "Închizi alerta și încheii sesiunea? Alerta deja emisă rămâne în istoric ca rezolvată."
                      : "Încheii sesiunea Vesta?"}
                  </p>
                  <button
                    type="button"
                    className="btn btn--outline"
                    data-testid="end-confirm"
                    onClick={() => void (state === "ALERT" ? companion.resolveAlert() : companion.cancel("USER_CANCELLED"))}
                    disabled={companion.actions.cancel.status === "sending" || companion.actions.resolve.status === "sending"}
                  >
                    Da, încheie
                  </button>
                  <button type="button" className="btn btn--ghost" onClick={() => setConfirmEnd(false)}>
                    Nu
                  </button>
                </div>
              ) : (
                <button type="button" className="btn btn--outline" data-testid="end" onClick={() => setConfirmEnd(true)}>
                  {state === "ALERT" ? "Închide alerta" : "Încheie sesiunea"}
                </button>
              )}
            </div>
          )}
          {/* Outside the block above: the server's answer stays visible after the session ends. */}
          {paired && (
            <ActionStatus
              action={companion.actions.cancel}
              sending="Se trimite anularea…"
              confirmed={(at) => `Serverul a încheiat sesiunea la ${formatClock(at)}.`}
              failed="Sesiunea NU a fost încheiată."
            />
          )}
          {paired && (
            <ActionStatus
              action={companion.actions.resolve}
              sending="Se trimite închiderea alertei…"
              confirmed={(at) => `Serverul a închis alerta la ${formatClock(at)}. Ea rămâne în istoric ca rezolvată.`}
              failed="Alerta NU a fost închisă."
            />
          )}
        </section>

        {/* --- Voice ---------------------------------------------------------- */}
        <section className="c-section" aria-labelledby="voice-head">
          <h2 id="voice-head" className="c-head"><Headphones size={18} aria-hidden="true" /> Conversație vocală</h2>
          <p className={`c-status tone--${voicePhaseCopy[voice.phase].tone}`} role="status" data-testid="voice-status" data-phase={voice.phase}>
            <span className="tone-mark" aria-hidden="true" />
            {voicePhaseCopy[voice.phase].label}
            {voiceCtl.open && voice.mode && (
              <span className="c-status-sub">{voice.mode === "speaking" ? " · asistentul vorbește" : " · asistentul ascultă"}</span>
            )}
          </p>
          {voice.detail && <p className="c-text">{voice.detail}</p>}

          {voiceCtl.open || voice.phase === "connecting" ? (
            <button type="button" className="btn btn--outline" onClick={() => void voiceCtl.stop()}>
              Încheie conversația
            </button>
          ) : (
            <button
              type="button"
              className="btn btn--primary"
              data-testid="voice-start"
              onClick={() => void voiceCtl.start()}
              disabled={!paired || !unfinished || voiceCtl.busy || voice.phase === "scripted"}
            >
              {voice.phase === "off" || voice.phase === "ended" ? "Pornește conversația vocală" : "Reîncearcă conversația"}
            </button>
          )}
          <p className="c-hint">
            {!paired || !unfinished
              ? "Conversația pornește doar într-o sesiune activă, cu telefonul asociat."
              : "Microfonul pornește doar după ce apeși butonul; apăsarea joystickului nu îl pornește. Verifică faptul că sunetul iese prin căști."}
          </p>

          {voice.lastAgentText && (
            <blockquote className="agent-text">
              <p>„{voice.lastAgentText.text}”</p>
              <footer>
                Trimis de asistent la {formatClock(voice.lastAgentText.at)}. Textul nu confirmă că mesajul a fost auzit.
              </footer>
            </blockquote>
          )}
          {voice.phase === "scripted" && (
            <div className="agent-text agent-text--scripted">
              <p className="c-hint">Asistentul vocal nu este disponibil acum. Mesajele sunt afișate ca text:</p>
              <ul>
                {voice.scriptedLines.map((line) => <li key={line}>{line}</li>)}
              </ul>
            </div>
          )}
          {voice.devicesChangedAt && (
            <p className="notice" role="status">
              Dispozitivele audio s-au schimbat la {formatClock(voice.devicesChangedAt)}. Verifică dacă sunetul iese prin căști.
            </p>
          )}
          {(voice.phase === "disconnected" || voice.phase === "unavailable" || voice.phase === "playback_error" || voice.phase === "mic_error") && (
            <p className="c-hint c-hint--strong">Nu auzi asistentul? Folosește „Sunt bine” sau SOS: funcționează fără voce.</p>
          )}
        </section>

        {/* --- GPS ------------------------------------------------------------ */}
        <section className="c-section" aria-labelledby="gps-head">
          <h2 id="gps-head" className="c-head">
            <MapPin size={18} aria-hidden="true" /> Locație
            <span className={`fresh fresh--${fresh.toLowerCase()}`} data-testid="gps-freshness">{fresh}</span>
          </h2>
          {geo.gps.fix ? (
            <dl className="c-facts c-facts--gps">
              <div>
                <dt>Coordonate</dt>
                <dd>
                  <span className={`num ${geo.gps.fix.source === "simulated" ? "sample" : ""}`}>
                    {geo.gps.fix.lat.toFixed(5)}, {geo.gps.fix.lng.toFixed(5)}
                  </span>
                  {geo.gps.fix.source === "simulated" && <> <SourceTag source="simulated" label="Simulate" /></>}
                </dd>
              </div>
              <div><dt>Precizie</dt><dd className="num">{geo.gps.fix.accuracy != null ? `± ${Math.round(geo.gps.fix.accuracy)} m` : "neraportată"}</dd></div>
              <div>
                <dt>Actualizată</dt>
                <dd className="num">
                  {formatClock(geo.gps.fix.timestamp)} <span className="c-age">({formatAge(now - geo.gps.fix.timestamp)})</span>
                </dd>
              </div>
            </dl>
          ) : (
            <p className="c-text">Nicio poziție disponibilă.</p>
          )}
          {fresh === "STALE" && geo.gps.fix?.source === "device" && (
            <p className="notice notice--warning" role="status">Aceasta este ultima poziție cunoscută, nu poziția actuală.</p>
          )}
          {geo.gps.error && <p className="notice notice--warning" role="status">{geo.gps.error}</p>}

          <div className="btn-row">
            {geo.gps.permission === "granted" || geo.gps.permission === "requesting" ? (
              <button type="button" className="btn btn--outline" onClick={geo.stop}>Oprește locația</button>
            ) : (
              <button type="button" className="btn btn--primary" onClick={geo.startWatch} disabled={geo.gps.permission === "unsupported"}>
                {geo.gps.permission === "denied" ? "Încearcă din nou locația" : "Permite locația"}
              </button>
            )}
            {geo.gps.fix?.source === "simulated" ? (
              <button type="button" className="btn btn--ghost" onClick={geo.hideSimulated}>Ascunde coordonatele simulate</button>
            ) : (
              geo.gps.permission !== "granted" && (
                <button type="button" className="btn btn--ghost" onClick={geo.showSimulated}>Arată coordonate simulate</button>
              )
            )}
          </div>
          <label className="location-share" htmlFor="share-location">
            <input
              id="share-location"
              data-testid="location-share"
              type="checkbox"
              checked={shareLocation}
              disabled={!paired || !unfinished || geo.gps.permission !== "granted" || geo.gps.fix?.source !== "device"}
              onChange={(event) => {
                setShareLocation(event.target.checked);
                if (!event.target.checked) setLocationUploadState("idle");
              }}
            />
            Trimite pozițiile GPS către server pe durata sesiunii
          </label>
          {locationUploadState !== "idle" && (
            <p className="c-hint" role="status" data-testid="location-upload-status">
              {locationUploadState === "sending" && "Se trimite poziția către server…"}
              {locationUploadState === "sent" && "Poziția a fost primită de server."}
              {locationUploadState === "failed" && "Poziția nu a fost confirmată de server. Se va reîncerca la următoarea poziție GPS."}
            </p>
          )}
          <p className="c-hint">
            Permisiunea browserului și partajarea cu serverul sunt alegeri separate. Partajarea este oprită implicit și cere
            sesiune asociată; oprește-o oricând. Cu ecranul blocat, browserul poate opri GPS-ul.
          </p>
        </section>

        {/* --- Emergency contacts (chosen by the person, invited by SMS) ------- */}
        {access.grant && <EmergencyContacts grant={access.grant} onGrantLost={access.forget} />}

        {/* --- Connectivity --------------------------------------------------- */}
        <section className="c-section" aria-labelledby="conn-head">
          <h2 id="conn-head" className="c-head"><Wifi size={18} aria-hidden="true" /> Conexiune</h2>
          <dl className="c-facts">
            <div><dt>Flux server</dt><dd data-testid="conn-stream">{link.state}</dd></div>
            <div>
              <dt>Ultimul semnal</dt>
              <dd className="num">
                {!paired ? "—" : companion.heartbeat.lastOkAt ? formatAge(now - companion.heartbeat.lastOkAt) : "niciunul"}
                {paired && companion.heartbeat.consecutiveFailures > 0 && ` · ${companion.heartbeat.consecutiveFailures} eșuate`}
              </dd>
            </div>
            <div><dt>Pagina</dt><dd>{visibility.hidden ? "În fundal" : "Deschisă"}</dd></div>
            <div>
              <dt>Asociere</dt>
              <dd>
                {access.grant ? (
                  <>
                    <span className="num">{access.grant.deviceId}</span>{" "}
                    <button type="button" className="text-button" onClick={access.forget}>Uită</button>
                  </>
                ) : (
                  "neasociat"
                )}
              </dd>
            </div>
            <div>
              <dt>Rețea (indicativ)</dt>
              <dd>{typeof navigator !== "undefined" && navigator.onLine ? "Browserul raportează online" : "Browserul raportează offline"}</dd>
            </div>
          </dl>
          <p className="c-hint">
            Ține pagina deschisă și ecranul pornit în timpul sesiunii. Cu ecranul blocat, browserul poate suspenda microfonul,
            GPS-ul și conexiunea. La revenire, Vesta cere starea actuală de la server.
          </p>
        </section>
      </main>
    </div>
  );
}
