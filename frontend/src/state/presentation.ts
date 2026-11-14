import type { AlertReason, SessionState, SessionView } from "../api/contract";
import type { SystemPower } from "./liveState";
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

/** Backend AlertReason / AlertIncidentType codes (v2), plus the v1 lower-case spellings. */
const alertReasonCopy: Record<string, string> = {
  SOS_EXPLICIT: "SOS cerut explicit",
  EXPLICIT_SOS: "SOS cerut explicit",
  MISSED_CHECK_INS: "Verificări fără răspuns",
  explicit_sos: "SOS cerut explicit",
  missed_check_ins: "Verificări fără răspuns",
};

export function alertReasonLabel(reason: AlertReason | null | undefined): string {
  if (!reason) return "Motiv neraportat";
  return alertReasonCopy[reason] ?? `Motiv: ${reason}`;
}

const resolutionCopy: Record<string, string> = {
  USER_CONFIRMED_SAFE: "închisă de utilizatoare",
  ACCIDENTAL_ACTIVATION: "activare accidentală",
  ALERT_RESOLVED: "închisă explicit",
};

export function resolutionLabel(action: string | null | undefined): string | null {
  if (!action) return null;
  return resolutionCopy[action] ?? action;
}

type IssueKind = "phone" | "voice" | "power" | "gps" | "other";

/** Every TechnicalIssue in backend/app/safety/models.py; unknown codes are shown as-is. */
const issueCopy: Record<string, { label: string; kind: IssueKind }> = {
  network_disconnected: { label: "Telefonul nu a mai trimis semnal", kind: "phone" },
  page_hidden: { label: "Pagina Vesta este în fundal pe telefon", kind: "phone" },
  voice_unavailable: { label: "Serviciul vocal indisponibil", kind: "voice" },
  voice_timeout: { label: "Serviciul vocal nu a răspuns la timp", kind: "voice" },
  microphone_denied: { label: "Microfon refuzat pe telefon", kind: "voice" },
  audio_interrupted: { label: "Audio întrerupt", kind: "voice" },
  audio_disconnected: { label: "Audio deconectat", kind: "voice" },
  gps_stale: { label: "Poziție GPS veche", kind: "gps" },
  gps_unavailable: { label: "GPS indisponibil", kind: "gps" },
  low_voltage: { label: "Tensiune scăzută (LOW_VOLTAGE)", kind: "power" },
  power_unstable: { label: "Alimentare instabilă (POWER_UNSTABLE)", kind: "power" },
  power_status_unknown: { label: "Starea alimentării necunoscută (POWER_STATUS_UNKNOWN)", kind: "power" },
  process_restarted: { label: "Serverul a repornit în timpul sesiunii", kind: "other" },
};

export function issueLabel(code: string): string {
  return issueCopy[code.toLowerCase()]?.label ?? `Problemă tehnică: ${code}`;
}

export function issuesOfKind(session: SessionView | null, kind: IssueKind): string[] {
  if (!session) return [];
  return session.active_technical_issues.filter((code) => (issueCopy[code.toLowerCase()]?.kind ?? "other") === kind);
}

export type Signal = { state: string; detail: string; tone: Tone; mark: "on" | "off" | "warn" | "unknown" };

/** Phone connectivity as the server sees it: the heartbeat lease plus reported issues. */
export function phoneSignal(session: SessionView | null, now: number): Signal {
  if (!session || session.state === "CANCELLED") {
    return { state: "Fără sesiune", detail: "Telefonul raportează conexiunea doar în timpul unei sesiuni.", tone: "neutral", mark: "unknown" };
  }
  if (session.active_technical_issues.includes("network_disconnected") || session.connectivity_status === "DISCONNECTED") {
    return { state: "Deconectat", detail: "Telefonul nu mai comunică cu serverul. Verificările automate sunt amânate.", tone: "danger", mark: "off" };
  }
  if (!session.client_last_seen_at) {
    return { state: "Neasociat", detail: "Telefonul nu s-a conectat încă la această sesiune.", tone: "warning", mark: "unknown" };
  }
  const lease = session.connection_lease_expires_at ? Date.parse(session.connection_lease_expires_at) : 0;
  if (lease > now) {
    if (session.page_visibility === "HIDDEN") {
      return {
        state: "Conectat · pagină în fundal",
        detail: `Ultimul semnal la ${formatClock(session.client_last_seen_at)}. Browserul poate opri microfonul și GPS-ul.`,
        tone: "warning",
        mark: "warn",
      };
    }
    return { state: "Conectat", detail: `Ultimul semnal la ${formatClock(session.client_last_seen_at)}.`, tone: "safe", mark: "on" };
  }
  return { state: "Fără semnal recent", detail: `Ultimul semnal la ${formatClock(session.client_last_seen_at)}.`, tone: "warning", mark: "warn" };
}

/** What the server knows about voice. It cannot know whether the person hears the assistant. */
export function voiceSignal(session: SessionView | null, voiceMode: "elevenlabs" | "demo" | null): Signal {
  if (!session || session.state === "CANCELLED") {
    if (voiceMode === "demo") {
      return { state: "Doar text", detail: "Asistentul vocal nu este disponibil; mesajele apar ca text, fără audio.", tone: "warning", mark: "warn" };
    }
    return { state: voiceMode ? "ElevenLabs configurat" : "Necunoscut", detail: "Conversația pornește din pagina telefonului, după acordul utilizatoarei.", tone: "neutral", mark: "unknown" };
  }
  const issues = issuesOfKind(session, "voice");
  if (issues.length > 0) {
    return { state: issueLabel(issues[0]), detail: "Raportat de telefon sau de server. SOS-ul rămâne disponibil.", tone: "danger", mark: "off" };
  }
  if (voiceMode === "demo") {
    return { state: "Doar text", detail: "Asistentul vocal nu este disponibil; mesajele apar ca text, fără audio.", tone: "warning", mark: "warn" };
  }
  if (session.audio_status === "CONNECTED") {
    return { state: "Audio conectat", detail: "Raportat de telefon. Serverul nu știe dacă asistentul este auzit.", tone: "safe", mark: "on" };
  }
  if (session.audio_status === "ERROR" || session.audio_status === "DISCONNECTED") {
    return { state: session.audio_status === "ERROR" ? "Eroare audio" : "Audio deconectat", detail: "Raportat de telefon. SOS-ul și „Sunt bine” rămân disponibile.", tone: "warning", mark: "warn" };
  }
  return { state: "Conversație nepornită", detail: "Conversația pornește din pagina telefonului, după acordul utilizatoarei.", tone: "neutral", mark: "unknown" };
}

/** GPS as reported by the phone. Coordinates themselves are not shown on this dashboard. */
export function gpsSignal(session: SessionView | null): Signal {
  if (!session || session.state === "CANCELLED") {
    return { state: "Fără sesiune", detail: "Telefonul raportează GPS-ul doar în timpul unei sesiuni.", tone: "neutral", mark: "unknown" };
  }
  if (session.gps_status === "GPS_AVAILABLE") {
    return { state: "Disponibil", detail: "Telefonul are o poziție recentă. Coordonatele nu sunt afișate în acest panou.", tone: "safe", mark: "on" };
  }
  if (session.gps_status === "GPS_STALE") {
    return { state: "Poziție veche", detail: "Telefonul are doar o poziție mai veche de 30 de secunde.", tone: "warning", mark: "warn" };
  }
  return { state: "Indisponibil", detail: "Telefonul nu a raportat o poziție. SOS-ul funcționează și fără GPS.", tone: "neutral", mark: "unknown" };
}

const powerCopy: Record<string, { state: string; tone: Tone; mark: Signal["mark"] }> = {
  POWER_OK: { state: "Normală", tone: "safe", mark: "on" },
  POWER_UNSTABLE: { state: "Alimentare instabilă (POWER_UNSTABLE)", tone: "warning", mark: "warn" },
  LOW_VOLTAGE: { state: "Tensiune scăzută (LOW_VOLTAGE)", tone: "warning", mark: "warn" },
};

/** Raspberry Pi power: the monitor's own report wins over the session field. No battery %. */
export function powerSignal(session: SessionView | null, system: SystemPower | null): Signal {
  const issues = issuesOfKind(session, "power");
  if (issues.length > 0) {
    return { state: issueLabel(issues[0]), detail: "Avertizare tehnică primită de la server.", tone: "warning", mark: "warn" };
  }
  const reported = system?.state ?? (session && session.power_status !== "POWER_STATUS_UNKNOWN" ? session.power_status : null);
  const copy = reported ? powerCopy[reported] : undefined;
  if (copy) {
    return {
      ...copy,
      detail: system ? `Raportat de monitorul de alimentare la ${formatClock(system.at)}.` : "Raportat de server pentru această sesiune.",
    };
  }
  return { state: "Neraportată", detail: "Starea alimentării dispozitivului nu este raportată.", tone: "neutral", mark: "unknown" };
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
  "session.already_active": { title: "Activare repetată: sesiunea existentă continuă", kind: "system" },
  "session.recovered": { title: "Sesiune reluată după repornirea serverului", kind: "warning" },
  "safety.check_in_delivered": { title: "Verificarea a ajuns la utilizatoare", kind: "check" },
  "safety.check_in_undelivered": { title: "Verificarea nu a putut fi livrată", kind: "warning" },
  "safety.alert_resolved": { title: "Alertă închisă", kind: "success" },
  "alert.resolved": { title: "Alertă închisă (păstrată în istoric)", kind: "success" },
  "alert.notification_requested": { title: "Notificarea contactului a fost cerută", kind: "alert" },
  "contact.notification": { title: "Notificare pentru contact înregistrată", kind: "system" },
  "client.status_changed": { title: "Starea telefonului actualizată", kind: "system" },
  "voice.status_changed": { title: "Starea conversației vocale actualizată", kind: "system" },
  "technical.power": { title: "Starea alimentării Raspberry Pi", kind: "warning" },
  "system.recovered": { title: "Serverul a repornit", kind: "warning" },
};
