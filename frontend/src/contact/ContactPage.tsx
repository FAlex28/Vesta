import { Bell, BellOff, Navigation, Phone, ShieldCheck } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import type { AccessGrant } from "../api/accessStore";
import { api, ApiError } from "../api/client";
import { CONTACT_POLL_MS } from "../api/config";
import { USER_RESOLUTION_ACTION, type ContactAlert, type ContactStatusResponse } from "../api/contract";
import { useOpenGrant } from "../companion/useCompanionAccess";
import { AlertList } from "../components/AlertList";
import { BrandLockup } from "../components/BrandLockup";
import { LinkBadge } from "../components/LinkBadge";
import { SectionHead } from "../components/SectionHead";
import { StatusOrb } from "../components/StatusOrb";
import { TIMISOARA_TEST_POINT } from "../data/dashboardData";
import { useNow } from "../services/session";
import type { AlertRecord } from "../state/liveState";
import { alertReasonLabel, formatAge, formatClock } from "../state/presentation";
import type { LiveLink } from "../state/useLiveConnection";
import { usePageVisibility } from "../state/usePageVisibility";
import { usePush, type PushState } from "./usePush";

const CityMap = lazy(() => import("../components/CityMap").then((module) => ({ default: module.CityMap })));

/*
 * The trusted contact's screen: the dispatcher's layout, simplified. It opens without any code
 * (server in VESTA_OPEN_ACCESS mode). During an active alert it shows the person's live position
 * (from the GPS on her Vesta device, or her phone as backup), refreshed every few seconds, with
 * "Sună la 112" and navigation. Afterwards only the alert history remains, without location.
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

/** How long "she is safe" stays at the top after she closed the alert. */
const SAFE_NOTICE_MS = 6 * 60 * 60 * 1000;

const pushCopy: Record<PushState, string> = {
  unsupported: "Browserul nu poate primi notificări. Ține pagina deschisă.",
  insecure: "Notificările nu merg pe această adresă. Ține pagina deschisă.",
  denied: "Notificările sunt blocate din setările browserului.",
  off: "Primește o notificare când se declanșează o alertă.",
  working: "Se activează…",
  on: "Notificări active.",
  failed: "Notificările nu au putut fi activate.",
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

  // Faster while an alert is active: the position moves.
  const alerting = Boolean(data?.alerts.some((item) => item.alert.status === "ACTIVE"));
  useEffect(() => {
    if (!grant) return;
    void poll();
    const timer = window.setInterval(poll, alerting ? 2_000 : CONTACT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [grant, poll, alerting]);

  return { data, link, poll } as const;
}

export default function ContactPage() {
  const { grant, problem, renew } = useOpenGrant("contact");
  const now = useNow();
  const { data, link, poll } = useContactFeed(grant, renew);
  const push = usePush(grant?.token ?? null, data ? data.push_enabled : null);
  usePageVisibility(() => void poll());

  const known = data !== null;
  const current = data?.alerts.find((item) => item.alert.status === "ACTIVE") ?? null;
  const records = (data?.alerts ?? []).map(toRecord);
  const person = data?.person_name || "Persoana pe care o susții";
  const clock = new Date(now).toLocaleTimeString("ro-RO", { hour: "2-digit", minute: "2-digit" });
  const location = current?.location ?? null;
  // The latest alert was closed by her ("I'm safe"): say so plainly, for a few hours.
  const latest = [...(data?.alerts ?? [])].sort((a, b) => Date.parse(b.alert.created_at) - Date.parse(a.alert.created_at))[0];
  const safeAt =
    !current && latest?.alert.status === "RESOLVED" && latest.alert.resolution_action === USER_RESOLUTION_ACTION && latest.alert.resolved_at
      ? latest.alert.resolved_at
      : null;
  const showSafe = safeAt !== null && now - Date.parse(safeAt) < SAFE_NOTICE_MS;
  const tone = current ? "danger" : known ? "safe" : "neutral";

  return (
    <div className={`app contact-board ${current ? "app--alert" : "app--idle"}`}>
      <header className="masthead">
        <div className="masthead-inner">
          <BrandLockup caption="Persoană de încredere" />
          <div className="masthead-end">
            <span className="masthead-clock num" aria-label={`Ora ${clock}`}>{clock}</span>
            <LinkBadge link={link} label="Server" />
          </div>
        </div>
      </header>

      <main className="page">
        <h1 className="sr-only">Vesta pentru persoana de încredere</h1>
        {problem === "disabled" && (
          <p className="notice notice--warning" role="alert" data-testid="contact-problem">
            Pagina nu se poate conecta la server.
          </p>
        )}
        {problem === "network" && <p className="notice notice--warning" role="alert">Serverul nu răspunde. Se reîncearcă.</p>}
        {known && link.state !== "CONNECTED" && (
          <p className="stale-banner" role="alert">
            <strong>Legătura cu serverul s-a întrerupt</strong> ({formatClock(data?.generated_at)}).
          </p>
        )}

        {showSafe && (
          <p className="notice safe-notice" role="status" data-testid="contact-safe-notice">
            <ShieldCheck size={20} aria-hidden="true" />
            <span>
              <strong>{data?.person_name ? `${data.person_name} este în siguranță.` : "Este în siguranță."}</strong> A închis
              singură alerta de pe telefon la <span className="num">{formatClock(safeAt)}</span>.
            </span>
          </p>
        )}

        <section className={`watch tone--${tone}`} aria-labelledby="contact-head" aria-live="assertive">
          <div className="watch-hero">
            <StatusOrb state={!known ? null : current ? "ALERT" : "IDLE"} />
            <div className="watch-head">
              <p className="watch-eyebrow">{person}</p>
              <h2 id="contact-head" className="watch-title" data-testid="contact-state">
                <span className="watch-title-text">{!known ? "Se așteaptă serverul" : current ? "Alertă activă" : "Nicio alertă activă"}</span>
              </h2>
              <p className="watch-detail">
                {!known
                  ? "Se conectează…"
                  : current
                    ? `${alertReasonLabel(current.alert_reason ?? current.alert.incident_type)} · ${formatClock(current.alert.created_at)}. Dispeceratul a fost anunțat. Sun-o; dacă nu răspunde, sună la 112.`
                    : "Totul este în regulă."}
              </p>
            </div>
            {current && (
              <a className="btn btn--sos btn--xl call-112" href="tel:112" data-testid="call-112">
                <Phone aria-hidden="true" /> Sună la 112
              </a>
            )}
          </div>
          {current && (
            <dl className="watch-facts">
              <div>
                <dt>Alertă emisă</dt>
                <dd className="num">{formatClock(current.alert.created_at)}</dd>
              </div>
              <div>
                <dt>Locație actualizată</dt>
                <dd className="num">{location ? formatAge(now - Date.parse(location.captured_at)) : "—"}</dd>
              </div>
              <div>
                <dt>Sursa locației</dt>
                <dd data-testid="contact-location-source">
                  {location ? (location.source === "device_gps" ? "GPS dispozitiv" : "GPS telefon") : "—"}
                </dd>
              </div>
            </dl>
          )}
        </section>

        <div className="board">
          <div className="board-col board-col--main">
            <section className={`panel area-map ${location ? "area-map--live" : ""}`} aria-labelledby="contact-location">
              <SectionHead title="Locație" id="contact-location">
                {current && (
                  <span className={`fresh fresh--${current.location_status.toLowerCase()}`} data-testid="contact-location-status">
                    {current.location_status}
                  </span>
                )}
              </SectionHead>
              <div className="map-frame">
                <p className={`map-tag ${location ? "map-tag--live" : ""}`}>
                  {location ? (
                    <><span className="live-dot" aria-hidden="true" /> În direct</>
                  ) : (
                    "Locația apare doar în timpul unei alerte"
                  )}
                </p>
                <Suspense fallback={<div className="map-loading">Se încarcă harta…</div>}>
                  {location ? (
                    <CityMap point={[location.latitude, location.longitude]} state="ALERT" kind="device" accuracy={location.accuracy_meters} />
                  ) : (
                    <CityMap point={TIMISOARA_TEST_POINT} state={null} kind="area" />
                  )}
                </Suspense>
              </div>
              {location ? (
                <>
                  <dl className="loc-facts">
                    <div>
                      <dt>Coordonate</dt>
                      <dd className="num" data-testid="contact-coordinates">
                        {location.latitude.toFixed(5)}, {location.longitude.toFixed(5)}
                      </dd>
                    </div>
                    <div>
                      <dt>Precizie</dt>
                      <dd className="num">{location.accuracy_meters != null ? `± ${Math.round(location.accuracy_meters)} m` : "neraportată"}</dd>
                    </div>
                    <div>
                      <dt>Captată</dt>
                      <dd className="num">{formatClock(location.captured_at)}</dd>
                    </div>
                  </dl>
                  {location.accuracy_meters != null && location.accuracy_meters > 100 && (
                    <p className="notice notice--warning" role="status" data-testid="contact-location-imprecise">
                      Poziție imprecisă (± {Math.round(location.accuracy_meters)} m).
                    </p>
                  )}
                  {location.is_stale && <p className="notice notice--warning" role="status">Poziția nu s-a mai actualizat de un minut.</p>}
                  <a
                    className="btn btn--primary"
                    href={`https://www.google.com/maps/dir/?api=1&destination=${location.latitude},${location.longitude}`}
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    <Navigation size={18} aria-hidden="true" /> Navighează până acolo
                  </a>
                </>
              ) : (
                current && <p className="panel-note">Telefonul ei nu a trimis încă locația.</p>
              )}
            </section>
          </div>

          <div className="board-col board-col--side">
            <section className="panel" aria-labelledby="contact-alerts">
              <SectionHead title="Alerte" id="contact-alerts" />
              <AlertList alerts={records} emptyText={known ? "Nicio alertă." : "Se conectează…"} />
            </section>

            <section className="panel" aria-labelledby="contact-push">
              <SectionHead title="Notificări" id="contact-push" />
              <p
                className={`c-status tone--${push.state === "on" ? "safe" : push.state === "off" || push.state === "working" ? "neutral" : "warning"}`}
                data-testid="push-state"
                data-state={push.state}
              >
                {push.state === "on" ? <Bell size={16} aria-hidden="true" /> : <BellOff size={16} aria-hidden="true" />}
                {pushCopy[push.state]}
              </p>
              {(push.state === "off" || push.state === "failed") && (
                <button type="button" className="btn btn--primary" onClick={() => void push.enable()} data-testid="push-enable">
                  <Bell size={18} aria-hidden="true" /> Permite notificările
                </button>
              )}
              {push.state === "on" && (
                <button type="button" className="btn btn--ghost" onClick={() => void push.disable()}>
                  Oprește notificările pe acest telefon
                </button>
              )}
            </section>
          </div>
        </div>
      </main>
    </div>
  );
}
