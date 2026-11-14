/*
 * Runtime configuration from Vite env variables. No secrets live here: the control token is
 * held only in memory (see services/credential.ts) and the ElevenLabs API key never reaches
 * the browser.
 *
 * VITE_VESTA_API_URL         Backend origin, e.g. http://192.168.1.20:8000. Empty = same origin
 *                            (the Vite dev server proxies /api, /ws and /health to the backend).
 * VITE_VESTA_DEVICE_ID       Device whose session the companion binds to when several exist.
 * VITE_VESTA_MANUAL_PAIRING  "true" also shows the test-only form (session id + control token)
 *                            on /companion. Normal pairing uses a one-time code from the dashboard.
 */
const env = import.meta.env;

const trimSlash = (value: string) => value.replace(/\/+$/, "");

export const apiBaseUrl = trimSlash((env.VITE_VESTA_API_URL as string | undefined)?.trim() ?? "");

export function wsUrl(path = "/ws/events"): string {
  const base = apiBaseUrl || window.location.origin;
  const url = new URL(path, base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

export const configuredDeviceId = ((env.VITE_VESTA_DEVICE_ID as string | undefined) ?? "").trim() || null;

export const manualPairingEnabled = env.VITE_VESTA_MANUAL_PAIRING === "true";

/** Every 15 s; the backend marks the phone disconnected after 45 s without one (VESTA_CLIENT_HEARTBEAT_TIMEOUT_SECONDS). */
export const HEARTBEAT_INTERVAL_MS = 15_000;
/** How often a paired phone asks whether the joystick started a session. */
export const COMPANION_POLL_MS = 3_000;
/** How often the trusted-contact page refreshes its alerts. */
export const CONTACT_POLL_MS = 4_000;
/** Backend sends system.heartbeat every 15 s by default; allow two missed beats. */
export const STREAM_SILENCE_MS = 35_000;
/** A GPS fix older than this is shown as STALE, never as current. */
export const GPS_STALE_AFTER_MS = 30_000;
