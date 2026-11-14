import { useCallback, useEffect, useRef, useState } from "react";
import { GPS_STALE_AFTER_MS } from "../api/config";
import { TIMISOARA_TEST_POINT } from "../data/dashboardData";

/*
 * Phone GPS through navigator.geolocation, only after the user taps "Permite locația".
 *
 * Freshness is computed from the fix's own timestamp:
 *   CURRENT      fix younger than GPS_STALE_AFTER_MS
 *   STALE        an older fix; shown with its age, never as the current position
 *   UNAVAILABLE  no fix (not requested, refused, unsupported, or no signal yet)
 *
 * The position stays on this phone. The current consent flow does not upload it to the server;
 * an upload would require a separate, explicit sharing choice.
 * A clearly labelled simulated point (the fixed test point) can be shown instead.
 */

export type GpsPermission = "not_requested" | "requesting" | "granted" | "denied" | "unsupported" | "insecure";
export type GpsFreshness = "CURRENT" | "STALE" | "UNAVAILABLE";

export type GpsFix = {
  lat: number;
  lng: number;
  accuracy: number | null;
  timestamp: number;
  source: "device" | "simulated";
};

export type GpsState = {
  permission: GpsPermission;
  fix: GpsFix | null;
  error: string | null;
};

export function freshnessOf(fix: GpsFix | null, now: number): GpsFreshness {
  if (!fix) return "UNAVAILABLE";
  return now - fix.timestamp <= GPS_STALE_AFTER_MS ? "CURRENT" : "STALE";
}

export function useGeolocation() {
  const supported = typeof navigator !== "undefined" && "geolocation" in navigator;
  const [state, setState] = useState<GpsState>({
    permission: !supported ? "unsupported" : "not_requested",
    fix: null,
    error: null,
  });
  const watchId = useRef<number | null>(null);

  const onPosition = useCallback((position: GeolocationPosition) => {
    setState({
      permission: "granted",
      error: null,
      fix: {
        lat: position.coords.latitude,
        lng: position.coords.longitude,
        accuracy: Number.isFinite(position.coords.accuracy) ? position.coords.accuracy : null,
        timestamp: position.timestamp || Date.now(),
        source: "device",
      },
    });
  }, []);

  const onError = useCallback((error: GeolocationPositionError) => {
    setState((current) => {
      if (error.code === error.PERMISSION_DENIED) {
        return { permission: "denied", fix: current.fix?.source === "device" ? current.fix : null, error: "Accesul la locație a fost refuzat." };
      }
      // Keep the last fix; its age makes it STALE rather than pretending it is current.
      const message = error.code === error.TIMEOUT ? "Semnal GPS insuficient (timp depășit)." : "Locația nu poate fi determinată acum.";
      return { ...current, permission: current.permission === "requesting" ? "granted" : current.permission, error: message };
    });
  }, []);

  const startWatch = useCallback(() => {
    if (!supported) return;
    if (!window.isSecureContext) {
      setState((current) => ({ ...current, permission: "insecure", error: "Browserul blochează locația pe o conexiune nesecurizată." }));
      return;
    }
    if (watchId.current !== null) navigator.geolocation.clearWatch(watchId.current);
    setState((current) => ({ ...current, permission: current.permission === "granted" ? "granted" : "requesting", error: null }));
    watchId.current = navigator.geolocation.watchPosition(onPosition, onError, {
      enableHighAccuracy: true,
      maximumAge: 10_000,
      timeout: 20_000,
    });
  }, [supported, onPosition, onError]);

  /** Ask once more for a fresh fix, e.g. when the page returns to the foreground. */
  const refresh = useCallback(() => {
    if (!supported || state.permission !== "granted") return;
    navigator.geolocation.getCurrentPosition(onPosition, onError, { enableHighAccuracy: true, maximumAge: 0, timeout: 20_000 });
  }, [supported, state.permission, onPosition, onError]);

  const stop = useCallback(() => {
    if (watchId.current !== null) navigator.geolocation.clearWatch(watchId.current);
    watchId.current = null;
    setState((current) => ({ ...current, permission: "not_requested", fix: null, error: null }));
  }, []);

  const showSimulated = useCallback(() => {
    const [lat, lng] = TIMISOARA_TEST_POINT;
    setState((current) => ({ ...current, fix: { lat, lng, accuracy: null, timestamp: Date.now(), source: "simulated" } }));
  }, []);

  const hideSimulated = useCallback(() => {
    setState((current) => (current.fix?.source === "simulated" ? { ...current, fix: null } : current));
  }, []);

  useEffect(
    () => () => {
      if (watchId.current !== null) navigator.geolocation.clearWatch(watchId.current);
    },
    [],
  );

  return { gps: state, startWatch, refresh, stop, showSimulated, hideSimulated } as const;
}
