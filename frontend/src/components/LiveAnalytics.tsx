import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { AnalyticsSnapshot } from "../api/contract";
import { formatClock } from "../state/presentation";
import { SectionHead } from "./SectionHead";

/* Live metrics read from Tiger Data: continuous aggregates (real-time), alert response
   times and how much the columnstore compression saves. Hidden without VESTA_PERSISTENCE=tiger. */

type Enabled = Extract<AnalyticsSnapshot, { enabled: true }>;

const COMPONENT_LABEL: Record<string, string> = {
  PHONE: "Telefon",
  PAGE: "Pagină",
  AUDIO: "Audio",
  VOICE_SERVICE: "Voce",
  GPS: "GPS",
  POWER: "Alimentare",
  SERVER: "Server",
  GPIO: "Buton",
};

function seconds(value: number | null): string {
  if (value == null) return "—";
  return value < 10 ? `${value.toFixed(1)} s` : `${Math.round(value)} s`;
}

function bytes(value: number | null): string {
  if (value == null) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} kB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function Tile({ label, value, tone, testId }: { label: string; value: string | number; tone?: "danger" | "warn" | "safe"; testId?: string }) {
  return (
    <div className={`an-tile ${tone ? `an-tile--${tone}` : ""}`} data-testid={testId}>
      <span className="an-value num">{value}</span>
      <span className="an-label">{label}</span>
    </div>
  );
}

function deviceTotals(data: Enabled) {
  const totals = new Map<string, { events: number; types: Map<string, number> }>();
  for (const row of data.device_health) {
    const entry = totals.get(row.component) ?? { events: 0, types: new Map() };
    entry.events += row.events;
    entry.types.set(row.event_type, (entry.types.get(row.event_type) ?? 0) + row.events);
    totals.set(row.component, entry);
  }
  return [...totals.entries()].sort((a, b) => b[1].events - a[1].events);
}

function gpsSummary(data: Enabled) {
  const fixes = data.gps_quality.reduce((n, g) => n + g.fixes, 0);
  const imprecise = data.gps_quality.reduce((n, g) => n + g.imprecise_fixes, 0);
  const weighted = data.gps_quality.reduce((n, g) => n + (g.avg_accuracy_m ?? 0) * g.fixes, 0);
  return { fixes, imprecise, accuracy: fixes ? weighted / fixes : null };
}

export function LiveAnalytics({ dashboardKey, refreshKey }: { dashboardKey: string | null; refreshKey: unknown }) {
  const [data, setData] = useState<AnalyticsSnapshot | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let stop = false;
    const load = () =>
      api
        .analytics(dashboardKey ?? undefined)
        .then((answer) => {
          if (stop) return;
          setData(answer);
          setFailed(false);
        })
        .catch(() => !stop && setFailed(true));
    void load();
    const timer = window.setInterval(load, 10_000);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [dashboardKey, refreshKey]);

  // Never loaded (old backend) or no Tiger Data configured: no panel at all.
  if (data === null || !data.enabled) return null;

  return (
    <section className="panel" id="analytics" aria-labelledby="analytics-head" data-testid="live-analytics">
      <SectionHead title="Analize live" id="analytics-head">
        <span className="panel-meta">
          Tiger Data · {formatClock(data.live.server_time)}
        </span>
      </SectionHead>
      {failed && (
        <p className="notice notice--warning" role="status">
          Tiger Data nu răspunde. Siguranța continuă în memorie; afișăm ultimele valori.
        </p>
      )}
      <Body data={data} />
    </section>
  );
}

function Body({ data }: { data: Enabled }) {
  const { live, alert_response: resp } = data;
  const devices = deviceTotals(data);
  const gps = gpsSummary(data);
  const before = data.storage.reduce((n, s) => n + (s.before_bytes ?? 0), 0);
  const after = data.storage.reduce((n, s) => n + (s.after_bytes ?? 0), 0);
  const saved = before > 0 ? Math.round((1 - after / before) * 100) : null;
  const activations = data.daily.filter((d) => d.event_type === "SESSION_ACTIVATED").reduce((n, d) => n + d.events, 0);

  return (
    <>
      <div className="an-tiles">
        <Tile label="Sesiuni deschise" value={live.open_sessions} testId="an-open" />
        <Tile label="Alerte active" value={live.active_alerts} tone={live.active_alerts ? "danger" : undefined} testId="an-alerts" />
        <Tile label="Notificări în curs" value={live.notifications_pending} tone={live.notifications_pending ? "warn" : undefined} />
        <Tile label="Telefoane conectate" value={`${live.phones_connected}${live.phones_degraded ? ` / ${live.phones_degraded} cu probleme` : ""}`} tone={live.phones_degraded ? "warn" : undefined} />
        <Tile label="Alertă → contact (mediană)" value={seconds(resp.p50_delivered_s)} />
        <Tile label="Alertă → contact (p95)" value={seconds(resp.p95_delivered_s)} />
        <Tile label="Activări, 7 zile" value={activations} />
        <Tile label="Spațiu economisit prin compresie" value={saved == null ? "—" : `${saved}%`} tone={saved != null && saved >= 90 ? "safe" : undefined} />
      </div>

      <div className="an-grid">
        <div>
          <h3 className="an-sub">Sănătate dispozitive · 24 h</h3>
          {devices.length === 0 ? (
            <p className="c-hint">Niciun eveniment tehnic.</p>
          ) : (
            <table className="an-table">
              <thead>
                <tr><th scope="col">Componentă</th><th scope="col">Evenimente</th><th scope="col">Cel mai des</th></tr>
              </thead>
              <tbody>
                {devices.map(([component, entry]) => {
                  const [top] = [...entry.types.entries()].sort((a, b) => b[1] - a[1]);
                  return (
                    <tr key={component}>
                      <th scope="row">{COMPONENT_LABEL[component] ?? component}</th>
                      <td className="num">{entry.events}</td>
                      <td>{top ? `${top[0]} (${top[1]})` : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        <div>
          <h3 className="an-sub">Senzor GPS · 24 h</h3>
          <dl className="an-dl">
            <div><dt>Poziții primite</dt><dd className="num">{gps.fixes}</dd></div>
            <div><dt>Precizie medie</dt><dd className="num">{gps.accuracy == null ? "—" : `± ${Math.round(gps.accuracy)} m`}</dd></div>
            <div><dt>Imprecise (&gt; 100 m)</dt><dd className="num">{gps.imprecise}</dd></div>
            <div><dt>Ultima poziție</dt><dd className="num">{live.last_gps_fix_at ? formatClock(live.last_gps_fix_at) : "—"}</dd></div>
          </dl>

          <h3 className="an-sub">Stocare (hypertables)</h3>
          <table className="an-table">
            <thead>
              <tr><th scope="col">Tabel</th><th scope="col">Chunks</th><th scope="col">Mărime</th><th scope="col">Comprimat</th></tr>
            </thead>
            <tbody>
              {data.storage.map((s) => (
                <tr key={s.name}>
                  <th scope="row">{s.name}</th>
                  <td className="num">{s.compressed_chunks ?? 0}/{s.chunks}</td>
                  <td className="num">{bytes(s.total_bytes)}</td>
                  <td className="num">
                    {s.before_bytes ? `${bytes(s.before_bytes)} → ${bytes(s.after_bytes)}` : "încă nimic"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <p className="panel-note">
        Agregate continue în timp real (fără refresh manual). Notificări: {resp.delivered}/{resp.notifications} livrate,{" "}
        {resp.viewed} văzute{resp.gave_up ? `, ${resp.gave_up} eșuate` : ""} în ultimele 7 zile.
      </p>
    </>
  );
}
