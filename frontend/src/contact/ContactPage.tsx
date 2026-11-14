import { Bell, BellOff, HeartHandshake, MapPin, Navigation, Phone } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { clearGrant, loadGrant, saveGrant, type AccessGrant } from "../api/accessStore";
import { api, ApiError } from "../api/client";
import { CONTACT_POLL_MS } from "../api/config";
import type { ContactAlert, ContactStatusResponse } from "../api/contract";
import { takeFragmentCode } from "../companion/useCompanionAccess";
import { AlertList } from "../components/AlertList";
import { BrandLockup } from "../components/BrandLockup";
import { LinkBadge } from "../components/LinkBadge";
import { StatusOrb } from "../components/StatusOrb";
import { useNow } from "../services/session";
import type { AlertRecord } from "../state/liveState";
import { alertReasonLabel, formatAge, formatClock } from "../state/presentation";
import type { LiveLink } from "../state/useLiveConnection";
import { usePageVisibility } from "../state/usePageVisibility";
import { usePush, type PushState } from "./usePush";

const CityMap = lazy(() => import("../components/CityMap").then((module) => ({ default: module.CityMap })));

/*
 * Emergency-contact page. The person who uses Vesta invited this contact from her own phone
 * (SMS with /contact#cod=...); the code is claimed once and removed from the address bar.
 * The page shows that person's alerts: ACTIVE with the last authorized location (only while
 * the alert is active), then RESOLVED with its closing time. With notifications allowed, an
 * alert also arrives as a push notification when this page is closed.
 */

const toRecord = (item: ContactAlert): AlertRecord => ({
  id: item.alert.id,
  sessionId: item.session_id,
  deviceId: "",
  status: item.alert.status === "ACTIVE" ? "ALERT" : "RESOLVED",
  reason: item.alert_reason,
  incidentType: item.alert.incident_type,
  raisedAt: item.alert.created_at,
  resolvedAt: item.alert.resolved_at,
  resolutionAction: item.alert.resolution_action,
});

const pushCopy: Record<PushState, string> = {
  unsupported: "Acest browser nu poate primi notificări. Folosește Chrome pe Android și ține pagina deschisă.",
  insecure: "Notificările cer o adresă securizată (https://). Deschide linkul primit prin SMS.",
  denied: "Notificările sunt blocate pentru acest site. Le poți permite din setările site-ului în Chrome.",
  off: "Permite notificările ca să afli de o alertă chiar dacă pagina este închisă.",
  working: "Se activează notificările…",
  on: "Notificările sunt active pe acest telefon.",
  failed: "Notificările nu au putut fi activate. Încearcă din nou.",
};

function useContactFeed(grant: AccessGrant | null, onUnauthorized: () => void) {
  const [data, setData] = useState<ContactStatusResponse | null>(null);
  const [link, setLink] = useState<LiveLink>({ state: "RECONNECTING", everConnected: false, lastMessageAt: null, failedAttempts: 0 });
  const viewed = useRef(new Set<string>());

  const poll = useCallback(async () => {
    if (!grant) return;
    try {
      const answer = await api.contactStatus(grant.token);
      setData(answer);
      setLink({ state: "CONNECTED", everConnected: true, lastMessageAt: Date.now(), failedAttempts: 0 });
      for (const item of answer.alerts) {
        if (item.alert.status !== "ACTIVE" || item.alert.viewed_at || viewed.current.has(item.alert.id)) continue;
        viewed.current.add(item.alert.id);
        api.contactViewed(item.alert.id, grant.token).catch(() => viewed.current.delete(item.alert.id));
      }
    } catch (error) {
      if (error instanceof ApiError && error.kind === "unauthorized") {
        onUnauthorized();
        return;
      }
      setLink((previous) => {
        const failedAttempts = previous.failedAttempts + 1;
        return { ...previous, failedAttempts, state: failedAttempts >= 3 ? "DISCONNECTED" : "RECONNECTING" };
      });
    }
  }, [grant, onUnauthorized]);

  useEffect(() => {
    if (!grant) return;
    void poll();
    const timer = window.setInterval(poll, CONTACT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [grant, poll]);

  return { data, link, poll } as const;
}

export default function ContactPage() {
  const [grant, setGrant] = useState<AccessGrant | null>(() => loadGrant("contact"));
  const [codeInput, setCodeInput] = useState("");
  const [claiming, setClaiming] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const now = useNow();

  const forget = useCallback(() => {
    clearGrant("contact");
    setGrant(null);
  }, []);
  const expired = useCallback(() => {
    forget();
    setProblem("Accesul a fost retras sau a expirat. Cere o invitație nouă persoanei pe care o susții.");
  }, [forget]);

  const { data, link, poll } = useContactFeed(grant, expired);
  const push = usePush(grant?.token ?? null, data ? data.push_enabled : null);
  usePageVisibility(() => void poll());

  const claim = useCallback(async (code: string) => {
    setClaiming(true);
    setProblem(null);
    try {
      const claimed = await api.claimContact(code);
      const next: AccessGrant = { scope: "contact", deviceId: claimed.device_id, token: claimed.access_token, expiresAt: claimed.expires_at };
      saveGrant(next);
      setGrant(next);
      setCodeInput("");
    } catch (error) {
      const failure = error instanceof ApiError ? error : null;
      setProblem(
        failure?.status === 429
          ? "Prea multe coduri greșite. Așteaptă un minut."
          : failure?.kind === "network"
            ? "Serverul nu a răspuns. Verifică conexiunea."
            : "Invitația nu este validă sau a expirat. Cere una nouă persoanei care te-a adăugat.",
      );
    } finally {
      setClaiming(false);
    }
  }, []);

  // The SMS link: /contact#cod=CODE claims on open (or when the fragment changes).
  useEffect(() => {
    const fromFragment = () => {
      const code = takeFragmentCode("cod");
      if (code) void claim(code);
    };
    fromFragment();
    window.addEventListener("hashchange", fromFragment);
    return () => window.removeEventListener("hashchange", fromFragment);
  }, [claim]);

  const onClaim = (event: FormEvent) => {
    event.preventDefault();
    void claim(codeInput);
  };

  const known = data !== null;
  const active = data?.alerts.filter((item) => item.alert.status === "ACTIVE") ?? [];
  const current = active[0] ?? null;
  const records = (data?.alerts ?? []).map(toRecord);
  const person = data?.person_name || "Persoana pe care o susții";

  return (
    <div className={`companion contact ${current ? "contact--alert" : ""}`}>
      <header className="companion-bar">
        <BrandLockup caption="Contact de urgență" />
        {grant && <LinkBadge link={link} label="Server" />}
      </header>
      <main className="companion-main">
        <h1 className="sr-only">Alerte Vesta pentru contactul de urgență</h1>

        {!grant ? (
          <section className="c-section c-card welcome" aria-labelledby="contact-pair">
            <HeartHandshake className="welcome-icon" aria-hidden="true" />
            <h2 id="contact-pair" className="welcome-title">Ai fost adăugat ca persoană de contact de urgență</h2>
            <p className="c-text">
              Deschide linkul din SMS-ul primit sau scrie codul de 8 caractere din mesaj. Vei vedea doar alertele persoanei
              care te-a invitat.
            </p>
            {problem && <p className="notice notice--warning" role="alert" data-testid="contact-problem">{problem}</p>}
            <form className="pair-form" onSubmit={onClaim}>
              <label htmlFor="contact-code">Codul din SMS</label>
              <input
                id="contact-code"
                type="text"
                autoCapitalize="characters"
                autoComplete="one-time-code"
                spellCheck={false}
                maxLength={16}
                value={codeInput}
                onChange={(event) => setCodeInput(event.target.value)}
              />
              <button type="submit" className="btn btn--primary" disabled={claiming || codeInput.trim().length < 4}>
                {claiming ? "Se verifică…" : "Deschide alertele"}
              </button>
            </form>
            <p className="c-hint">Vesta nu apelează 112 și nu trimite mesaje în numele tău.</p>
          </section>
        ) : (
          <>
            <section className={`c-state c-hero tone--${current ? "danger" : known ? "safe" : "neutral"}`} aria-live="assertive">
              <div className="c-hero-top">
                <StatusOrb state={!known ? null : current ? "ALERT" : "IDLE"} size="md" />
                <div className="c-hero-text">
                  <p className="c-eyebrow">{person}</p>
                  <h2 className="c-state-title" data-testid="contact-state">
                    {!known ? "Se așteaptă serverul" : current ? "Alertă activă" : "Nicio alertă activă"}
                  </h2>
                </div>
              </div>
              <p className="c-state-detail">
                {!known
                  ? "Pagina afișează alertele doar după ce răspunde serverul."
                  : current
                    ? `${alertReasonLabel(current.alert_reason ?? current.alert.incident_type)} · emisă la ${formatClock(current.alert.created_at)}. Încearcă să o contactezi direct. Dacă nu răspunde și crezi că e în pericol, sună la 112.`
                    : "Totul e liniștit. Dacă Vesta emite o alertă, apare aici și primești o notificare."}
              </p>
              {current && (
                <a className="btn btn--sos btn--xl call-112" href="tel:112">
                  <Phone aria-hidden="true" /> Sună la 112
                </a>
              )}
              {known && link.state !== "CONNECTED" && (
                <p className="notice notice--warning" role="alert">
                  Legătura cu serverul este întreruptă. Lista arată ultima stare primită ({formatClock(data?.generated_at)}).
                </p>
              )}
            </section>

            {current && (
              <section className="c-section c-card" aria-labelledby="contact-location">
                <h2 id="contact-location" className="c-head">
                  <MapPin size={18} aria-hidden="true" /> Ultima locație cunoscută
                  <span className={`fresh fresh--${current.location_status.toLowerCase()}`} data-testid="contact-location-status">
                    {current.location_status}
                  </span>
                </h2>
                {current.location ? (
                  <>
                    <div className="map-frame map-frame--contact">
                      <Suspense fallback={<div className="map-loading">Se încarcă harta…</div>}>
                        <CityMap
                          point={[current.location.latitude, current.location.longitude]}
                          state="ALERT"
                          kind="device"
                          accuracy={current.location.accuracy_meters}
                        />
                      </Suspense>
                    </div>
                    <dl className="c-facts c-facts--gps">
                      <div>
                        <dt>Coordonate</dt>
                        <dd className="num" data-testid="contact-coordinates">
                          {current.location.latitude.toFixed(5)}, {current.location.longitude.toFixed(5)}
                        </dd>
                      </div>
                      <div>
                        <dt>Precizie</dt>
                        <dd className="num">
                          {current.location.accuracy_meters != null ? `± ${Math.round(current.location.accuracy_meters)} m` : "neraportată"}
                        </dd>
                      </div>
                      <div>
                        <dt>Captată</dt>
                        <dd className="num">
                          {formatClock(current.location.captured_at)}{" "}
                          <span className="c-age">({formatAge(now - Date.parse(current.location.captured_at))})</span>
                        </dd>
                      </div>
                    </dl>
                    {current.location.is_stale && (
                      <p className="notice notice--warning" role="status">
                        Poziția este veche: persoana poate fi în alt loc acum.
                      </p>
                    )}
                    <a
                      className="btn btn--primary"
                      href={`https://www.google.com/maps/dir/?api=1&destination=${current.location.latitude},${current.location.longitude}`}
                      target="_blank"
                      rel="noreferrer noopener"
                    >
                      <Navigation size={18} aria-hidden="true" /> Navighează până acolo
                    </a>
                  </>
                ) : (
                  <p className="c-text">Telefonul nu a trimis o poziție autorizată. Alerta este valabilă și fără locație.</p>
                )}
              </section>
            )}

            <section className="c-section c-card" aria-labelledby="contact-push">
              <h2 id="contact-push" className="c-head">
                {push.state === "on" ? <Bell size={18} aria-hidden="true" /> : <BellOff size={18} aria-hidden="true" />} Notificări
              </h2>
              <p className={`c-status tone--${push.state === "on" ? "safe" : push.state === "off" || push.state === "working" ? "neutral" : "warning"}`} data-testid="push-state" data-state={push.state}>
                <span className="tone-mark" aria-hidden="true" />
                {pushCopy[push.state]}
              </p>
              {(push.state === "off" || push.state === "failed") && (
                <button type="button" className="btn btn--primary btn--xl" onClick={() => void push.enable()} data-testid="push-enable">
                  <Bell size={20} aria-hidden="true" /> Permite notificările
                </button>
              )}
              {push.state === "on" && (
                <button type="button" className="btn btn--ghost" onClick={() => void push.disable()}>
                  Oprește notificările pe acest telefon
                </button>
              )}
            </section>

            <section className="c-section" aria-labelledby="contact-alerts">
              <h2 id="contact-alerts" className="c-head">Istoric alerte</h2>
              <AlertList alerts={records} emptyText={known ? "Nicio alertă primită." : "Se așteaptă serverul."} />
              <p className="c-hint">
                O alertă închisă nu dispare: rămâne ca RESOLVED, cu ora închiderii. Locația nu mai este afișată după închidere.
              </p>
              <button type="button" className="btn btn--ghost" onClick={forget}>
                Renunță la acces pe acest telefon
              </button>
            </section>
          </>
        )}
      </main>
    </div>
  );
}
