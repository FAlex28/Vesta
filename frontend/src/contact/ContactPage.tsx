import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { clearGrant, loadGrant, saveGrant, type AccessGrant } from "../api/accessStore";
import { api, ApiError } from "../api/client";
import { CONTACT_POLL_MS } from "../api/config";
import type { ContactAlert, ContactStatusResponse } from "../api/contract";
import { AlertList } from "../components/AlertList";
import { BrandLockup } from "../components/BrandLockup";
import { LinkBadge } from "../components/LinkBadge";
import { useNow } from "../services/session";
import type { AlertRecord } from "../state/liveState";
import { alertReasonLabel, formatAge, formatClock } from "../state/presentation";
import type { LiveLink } from "../state/useLiveConnection";
import { usePageVisibility } from "../state/usePageVisibility";

const CityMap = lazy(() => import("../components/CityMap").then((module) => ({ default: module.CityMap })));

/*
 * Trusted-contact page. Access comes from a one-time code created by the operator on the
 * dashboard; the resulting grant is limited to one device and expires. The page shows that
 * device's alerts: ACTIVE with the last authorized location (only while the alert is active),
 * then RESOLVED with its closing time. Opening an active alert is reported to the server as
 * "viewed", which is distinct from "notification requested". It sends no SMS or calls.
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
    setProblem("Accesul a expirat sau a fost retras. Cere un cod nou persoanei pe care o susții.");
  }, [forget]);

  const { data, link, poll } = useContactFeed(grant, expired);
  usePageVisibility(() => void poll());

  const onClaim = async (event: FormEvent) => {
    event.preventDefault();
    setClaiming(true);
    setProblem(null);
    try {
      const claimed = await api.claimPairingCode(codeInput);
      if (claimed.scope !== "contact") {
        setProblem("Acesta este un cod pentru telefonul persoanei, nu pentru contact.");
        return;
      }
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
            : "Codul nu este valid sau a expirat.",
      );
    } finally {
      setClaiming(false);
    }
  };

  const known = data !== null;
  const active = data?.alerts.filter((item) => item.alert.status === "ACTIVE") ?? [];
  const current = active[0] ?? null;
  const records = (data?.alerts ?? []).map(toRecord);

  return (
    <div className="companion contact">
      <header className="companion-bar">
        <BrandLockup caption="Contact de încredere" />
        {grant && <LinkBadge link={link} label="Server" />}
      </header>
      <main className="companion-main">
        <h1 className="sr-only">Alerte Vesta pentru contactul de încredere</h1>
        <p className="notice" role="note">
          Această pagină nu trimite SMS, apeluri sau notificări și nu apelează 112. Ține-o deschisă ca să vezi alertele.
        </p>

        {!grant ? (
          <section className="c-section" aria-labelledby="contact-pair">
            <h2 id="contact-pair" className="c-head">Acces pentru contactul de încredere</h2>
            {problem && <p className="notice notice--warning" role="alert" data-testid="contact-problem">{problem}</p>}
            <form className="pair-form" onSubmit={onClaim}>
              <label htmlFor="contact-code">Codul de acces primit</label>
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
              <p className="c-hint">
                Codul se folosește o singură dată. Accesul arată doar alertele acestei persoane și expiră după 24 de ore.
              </p>
            </form>
          </section>
        ) : (
          <>
            <section className={`c-state tone--${current ? "danger" : "neutral"}`} aria-live="assertive">
              <p className="c-eyebrow">Situația curentă</p>
              <h2 className="c-state-title" data-testid="contact-state">
                <span className="tone-mark" aria-hidden="true" />
                {!known ? "Se așteaptă serverul" : current ? "Alertă activă" : "Nicio alertă activă"}
              </h2>
              <p className="c-state-detail">
                {!known
                  ? "Pagina afișează alertele doar după ce răspunde serverul."
                  : current
                    ? `${alertReasonLabel(current.alert_reason ?? current.alert.incident_type)} · emisă la ${formatClock(current.alert.created_at)}. Încearcă să contactezi persoana direct. Dacă nu răspunde și crezi că e în pericol, sună la 112.`
                    : "Alertele închise rămân mai jos, cu ora închiderii."}
              </p>
              {known && link.state !== "CONNECTED" && (
                <p className="notice notice--warning" role="alert">
                  Legătura cu serverul este întreruptă. Lista arată ultima stare primită ({formatClock(data?.generated_at)}).
                </p>
              )}
            </section>

            {current && (
              <section className="c-section" aria-labelledby="contact-location">
                <h2 id="contact-location" className="c-head">
                  Ultima locație cunoscută
                  <span className={`fresh fresh--${current.location_status.toLowerCase()}`} data-testid="contact-location-status">
                    {current.location_status}
                  </span>
                </h2>
                {current.location ? (
                  <>
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
                    <div className="map-frame">
                      <Suspense fallback={<div className="map-loading">Se încarcă harta…</div>}>
                        <CityMap
                          point={[current.location.latitude, current.location.longitude]}
                          state="ALERT"
                          kind="device"
                          accuracy={current.location.accuracy_meters}
                        />
                      </Suspense>
                    </div>
                    <p className="c-hint">
                      <a href={`https://www.openstreetmap.org/?mlat=${current.location.latitude}&mlon=${current.location.longitude}#map=17/${current.location.latitude}/${current.location.longitude}`} target="_blank" rel="noreferrer noopener">
                        Deschide în hartă
                      </a>
                    </p>
                  </>
                ) : (
                  <p className="c-text">Telefonul nu a trimis o poziție autorizată. Alerta este valabilă și fără locație.</p>
                )}
              </section>
            )}

            <section className="c-section" aria-labelledby="contact-alerts">
              <h2 id="contact-alerts" className="c-head">Alerte</h2>
              <AlertList alerts={records} emptyText={known ? "Nicio alertă primită." : "Se așteaptă serverul."} />
              <p className="c-hint">
                O alertă închisă nu dispare: rămâne în listă ca RESOLVED, cu ora închiderii. Locația nu mai este afișată după
                închidere.
              </p>
              <button type="button" className="btn btn--ghost" onClick={forget}>
                Renunță la acces pe acest dispozitiv
              </button>
            </section>
          </>
        )}
      </main>
    </div>
  );
}
