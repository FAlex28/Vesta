import type { AlertReason, SessionState, SessionView } from "../api/contract";
import type { LinkState } from "./useLiveConnection";

export type Tone = "neutral" | "safe" | "warning" | "danger";

export const stateCopy: Record<SessionState, { title: string; detail: string; tone: Tone }> = {
  IDLE: {
    title: "Sistem pregătit",
    detail: "Nicio sesiune în desfășurare. Vesta pornește după 5 secunde de apăsare continuă pe joystick.",
    tone: "neutral",
  },
  ACTIVE: {
    title: "Sesiune în desfășurare",
    detail: "Serverul a confirmat activarea. Vesta va cere periodic confirmarea siguranței.",
    tone: "safe",
  },
  CHECK_IN: {
    title: "Se cere confirmarea siguranței",
    detail: "Serverul așteaptă un răspuns: vocal sau prin butonul „Sunt bine”.",
    tone: "warning",
  },
  UNCONFIRMED: {
    title: "Confirmarea nu a fost primită",
    detail: "Serverul va repeta verificarea. Dacă nici aceasta nu primește răspuns, sesiunea trece în alertă.",
    tone: "warning",
  },
  ALERT: {
    title: "Sesiune în alertă",
    detail: "Serverul a emis alerta către contactul de încredere. Vesta nu apelează 112.",
    tone: "danger",
  },
  CANCELLED: {
    title: "Sesiune încheiată",
    detail: "Sesiunea a fost anulată sau încheiată. O nouă activare pornește o sesiune nouă.",
    tone: "neutral",
  },
};

export const linkCopy: Record<LinkState, { label: string; tone: Tone }> = {
  CONNECTED: { label: "Conectat", tone: "safe" },
  RECONNECTING: { label: "Se reconectează", tone: "warning" },
  DISCONNECTED: { label: "Deconectat", tone: "danger" },
};

export const alertReasonCopy: Record<AlertReason, string> = {
  explicit_sos: "SOS cerut explicit",
  missed_check_ins: "Verificări fără răspuns",
};

/** Contract issues plus the power codes named in the brief; unknown codes are shown as-is. */
const issueCopy: Record<string, { label: string; kind: "phone" | "voice" | "power" }> = {
  network_disconnected: { label: "Telefonul nu a mai trimis semnal", kind: "phone" },
  voice_unavailable: { label: "Serviciul vocal indisponibil", kind: "voice" },
  microphone_denied: { label: "Microfon refuzat pe telefon", kind: "voice" },
  audio_interrupted: { label: "Audio întrerupt", kind: "voice" },
  low_voltage: { label: "Tensiune scăzută (LOW_VOLTAGE)", kind: "power" },
  power_unstable: { label: "Alimentare instabilă (POWER_UNSTABLE)", kind: "power" },
  power_status_unknown: { label: "Starea alimentării necunoscută (POWER_STATUS_UNKNOWN)", kind: "power" },
};

export function issueLabel(code: string): string {
  return issueCopy[code.toLowerCase()]?.label ?? `Problemă tehnică: ${code}`;
}

export function issuesOfKind(session: SessionView | null, kind: "phone" | "voice" | "power" | "other"): string[] {
  if (!session) return [];
  return session.active_technical_issues.filter((code) => (issueCopy[code.toLowerCase()]?.kind ?? "other") === kind);
}

export type Signal = { state: string; detail: string; tone: Tone; mark: "on" | "off" | "warn" | "unknown" };

/** Phone connectivity as the server sees it: the heartbeat lease plus reported issues. */
export function phoneSignal(session: SessionView | null, now: number): Signal {
  if (!session || session.state === "CANCELLED") {
    return { state: "Fără sesiune", detail: "Telefonul raportează conexiunea doar în timpul unei sesiuni.", tone: "neutral", mark: "unknown" };
  }
  if (session.active_technical_issues.includes("network_disconnected")) {
    return { state: "Deconectat", detail: "Telefonul nu mai comunică cu serverul. Verificările automate sunt amânate.", tone: "danger", mark: "off" };
  }
  if (!session.client_last_seen_at) {
    return { state: "Neasociat", detail: "Telefonul nu s-a conectat încă la această sesiune.", tone: "warning", mark: "unknown" };
  }
  const lease = session.connection_lease_expires_at ? Date.parse(session.connection_lease_expires_at) : 0;
  if (lease > now) {
    return { state: "Conectat", detail: `Ultimul semnal la ${formatClock(session.client_last_seen_at)}.`, tone: "safe", mark: "on" };
  }
  return { state: "Fără semnal recent", detail: `Ultimul semnal la ${formatClock(session.client_last_seen_at)}.`, tone: "warning", mark: "warn" };
}

/** What the server knows about voice. It cannot know whether the person hears the assistant. */
export function voiceSignal(session: SessionView | null, voiceMode: "elevenlabs" | "demo" | null): Signal {
  const issues = issuesOfKind(session, "voice");
  if (issues.length > 0) {
    return { state: issueLabel(issues[0]), detail: "Raportat de telefon sau de server. SOS-ul rămâne disponibil.", tone: "danger", mark: "off" };
  }
  if (voiceMode === "demo") {
    return { state: "Doar text", detail: "Asistentul vocal nu este disponibil; mesajele apar ca text, fără audio.", tone: "warning", mark: "warn" };
  }
  if (!session || session.state === "CANCELLED") {
    return { state: voiceMode ? "ElevenLabs configurat" : "Necunoscut", detail: "Conversația pornește din pagina telefonului, după acordul utilizatoarei.", tone: "neutral", mark: "unknown" };
  }
  return { state: "Nicio problemă raportată", detail: "Serverul nu știe dacă asistentul este auzit; vezi pagina telefonului.", tone: "neutral", mark: "unknown" };
}

export function powerSignal(session: SessionView | null): Signal {
  const issues = issuesOfKind(session, "power");
  if (issues.length > 0) {
    return { state: issueLabel(issues[0]), detail: "Avertizare tehnică primită de la server.", tone: "warning", mark: "warn" };
  }
  return {
    state: "Neraportată",
    detail: "Starea alimentării dispozitivului nu este raportată.",
    tone: "neutral",
    mark: "unknown",
  };
}

/* --------------------------------------------------------------------------
   Time
   -------------------------------------------------------------------------- */

export function formatClock(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const date = typeof value === "number" ? new Date(value) : new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("ro-RO", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${pad(minutes)}:${pad(seconds)}`;
}

export function formatAge(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `acum ${seconds} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `acum ${minutes} min`;
  return `acum ${Math.round(minutes / 60)} h`;
}

export function sessionDuration(session: SessionView | null, now: number): number | null {
  if (!session) return null;
  const start = Date.parse(session.created_at);
  const end = session.state === "CANCELLED" ? Date.parse(session.updated_at) : now;
  return Number.isNaN(start) ? null : end - start;
}

export const eventCopy: Record<string, { title: string; kind: "system" | "check" | "warning" | "alert" | "success" }> = {
  "session.activated": { title: "Vesta activată (confirmat de server)", kind: "system" },
  "safety.check_in_requested": { title: "Verificare de siguranță cerută", kind: "check" },
  "safety.check_in_unconfirmed": { title: "Confirmarea nu a fost primită", kind: "warning" },
  "safety.check_in_confirmed": { title: "Siguranță confirmată", kind: "success" },
  "safety.check_in_deferred": { title: "Verificare amânată din motive tehnice", kind: "warning" },
  "safety.alerted": { title: "Alertă emisă", kind: "alert" },
  "session.cancelled": { title: "Sesiune încheiată", kind: "system" },
  "technical.issue_reported": { title: "Problemă tehnică raportată", kind: "warning" },
  "technical.issue_resolved": { title: "Problemă tehnică rezolvată", kind: "success" },
};
