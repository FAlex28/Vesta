import { Route } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { LocationPoint } from "../api/contract";
import { formatClock } from "../state/presentation";
import { SectionHead } from "./SectionHead";

/*
 * Where she has been during an alert: every position her phone (or the device GPS) sent, kept
 * with the recording. Never shown before an alert and never kept for a false alarm (server rule).
 */

const EARTH_M = 6_371_000;
const rad = (deg: number) => (deg * Math.PI) / 180;

export function metersBetween(a: LocationPoint, b: LocationPoint): number {
  const dLat = rad(b.latitude - a.latitude);
  const dLng = rad(b.longitude - a.longitude);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.latitude)) * Math.cos(rad(b.latitude)) * Math.sin(dLng / 2) ** 2;
  return 2 * EARTH_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** Distance walked, without the jitter of imprecise fixes. */
export function routeLength(points: LocationPoint[]): number {
  let total = 0;
  for (let i = 1; i < points.length; i += 1) {
    const a = points[i - 1];
    const b = points[i];
    if ((a.accuracy_meters ?? 0) > 100 || (b.accuracy_meters ?? 0) > 100) continue;
    const step = metersBetween(a, b);
    if (step > Math.max(5, Math.min(a.accuracy_meters ?? 5, b.accuracy_meters ?? 5))) total += step;
  }
  return total;
}

function formatMeters(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(1).replace(".", ",")} km` : `${Math.round(value)} m`;
}

/** null: nothing shared for this session (no alert yet, or a false alarm). */
export function useLocationHistory(sessionId: string | null, dashboardKey: string | null, live: boolean, refreshKey: unknown) {
  const [points, setPoints] = useState<LocationPoint[] | null>(null);
  useEffect(() => {
    if (!sessionId) {
      setPoints(null);
      return;
    }
    let stop = false;
    const load = () =>
      api
        .locationHistory(sessionId, dashboardKey ?? undefined)
        .then((answer) => !stop && setPoints(answer.points))
        .catch((error: unknown) => {
          if (!stop && error instanceof ApiError && error.kind === "not_found") setPoints(null);
        });
    void load();
    const timer = live ? window.setInterval(load, 5000) : undefined;
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [sessionId, dashboardKey, live, refreshKey]);
  return points;
}

const VISIBLE = 8;

/** The list of positions, newest first. */
export function LocationTrail({ points, presentation }: { points: LocationPoint[]; presentation: boolean }) {
  const [all, setAll] = useState(false);
  if (points.length === 0) return <p className="c-hint">Nicio poziție primită.</p>;
  const newestFirst = points.map((point, index) => ({ point, moved: index > 0 ? metersBetween(points[index - 1], point) : null })).reverse();
  const shown = all ? newestFirst : newestFirst.slice(0, VISIBLE);
  return (
    <>
      <ol className="trail" data-testid="location-trail">
        {shown.map(({ point, moved }, index) => (
          <li key={`${point.captured_at}-${index}`} className={`trail-row ${index === 0 ? "trail-row--latest" : ""}`}>
            <time className="num" dateTime={point.captured_at}>{formatClock(point.captured_at)}</time>
            <span className="num trail-coords">
              {presentation ? "ascunse" : `${point.latitude.toFixed(5)}, ${point.longitude.toFixed(5)}`}
            </span>
            <span className="trail-meta num">
              {point.accuracy_meters != null ? `± ${Math.round(point.accuracy_meters)} m` : "—"}
              {" · "}
              {point.source === "device_gps" ? "dispozitiv" : "telefon"}
              {moved !== null && moved >= 5 && <> · +{formatMeters(moved)}</>}
            </span>
          </li>
        ))}
      </ol>
      {points.length > VISIBLE && (
        <button type="button" className="text-button" onClick={() => setAll((value) => !value)}>
          {all ? "Arată mai puține" : `Arată toate (${points.length})`}
        </button>
      )}
    </>
  );
}

export function LocationHistoryPanel({ points, live, presentation }: { points: LocationPoint[]; live: boolean; presentation: boolean }) {
  const first = points[0];
  const distance = routeLength(points);
  return (
    <section className="panel area-route" id="route" aria-labelledby="route-head" data-testid="location-history">
      <SectionHead title="Istoric locație" id="route-head">
        <span className="panel-meta">{live ? "se actualizează" : "salvat"}</span>
      </SectionHead>
      {first && (
        <p className="route-summary" data-testid="route-summary">
          <Route size={16} aria-hidden="true" />
          <span>
            <span className="num">{points.length}</span> {points.length === 1 ? "poziție" : "poziții"} de la{" "}
            <span className="num">{formatClock(first.captured_at)}</span>
            {distance >= 5 && <> · aprox. <span className="num">{formatMeters(distance)}</span> parcurși</>}
          </span>
        </p>
      )}
      <LocationTrail points={points} presentation={presentation} />
    </section>
  );
}
