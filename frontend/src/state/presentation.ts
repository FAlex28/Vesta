import type { AlertReason, SessionState, SessionView } from "../api/contract";
import type { SystemPower } from "./liveState";
import type { LinkState } from "./useLiveConnection";

export type Tone = "neutral" | "safe" | "warning" | "danger";

export const stateCopy: Record<SessionState, { title: string; detail: string; tone: Tone }> = {
  IDLE: { title: "În așteptare", detail: "Nicio sesiune activă.", tone: "neutral" },
  ACTIVE: {
    title: "Sesiune activă",
    detail: "Persoana a pornit Vesta. Asistentul vorbește cu ea în căști.",
    tone: "safe",
  },
  CHECK_IN: { title: "Se așteaptă răspunsul", detail: "Vesta a întrebat-o dacă este bine.", tone: "warning" },
  UNCONFIRMED: {
    title: "Fără răspuns",
    detail: "Vesta întreabă din nou. Dacă nu răspunde, se declanșează alerta.",
    tone: "warning",
  },
  ALERT: {
    title: "Alertă",
    detail: "Persoana are nevoie de ajutor. Persoana de încredere a fost anunțată.",
    tone: "danger",
  },
  CANCELLED: { title: "Sesiune încheiată", detail: "Vesta a fost oprită.", tone: "neutral" },
};

export const linkCopy: Record<LinkState, { label: string; tone: Tone }> = {
  CONNECTED: { label: "Conectat", tone: "safe" },
  RECONNECTING: { label: "Se reconectează", tone: "warning" },
  DISCONNECTED: { label: "Deconectat", tone: "danger" },
};

/** Backend AlertReason / AlertIncidentType codes (v2), plus the v1 lower-case spellings. */
const alertReasonCopy: Record<string, string> = {
  SOS_EXPLICIT: "A cerut ajutor",
  EXPLICIT_SOS: "A cerut ajutor",
  MISSED_CHECK_INS: "Nu a răspuns",
  explicit_sos: "A cerut ajutor",
  missed_check_ins: "Nu a răspuns",
};

export function alertReasonLabel(reason: AlertReason | null | undefined): string {
  if (!reason) return "Alertă";
  return alertReasonCopy[reason] ?? `Motiv: ${reason}`;
}

const resolutionCopy: Record<string, string> = {
  USER_CONFIRMED_SAFE: "închisă de utilizatoare",
  ACCIDENTAL_ACTIVATION: "activare accidentală",
  ALERT_RESOLVED: "închisă explicit",
  FALSE_ALARM: "alarmă falsă",
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
    return { state: "Fără sesiune", detail: "", tone: "neutral", mark: "unknown" };
  }
  if (session.active_technical_issues.includes("network_disconnected") || session.connectivity_status === "DISCONNECTED") {
    return { state: "Deconectat", detail: "Telefonul nu mai trimite date.", tone: "danger", mark: "off" };
  }
  if (!session.client_last_seen_at) {
    return { state: "Se conectează", detail: "", tone: "warning", mark: "unknown" };
  }
  const lease = session.connection_lease_expires_at ? Date.parse(session.connection_lease_expires_at) : 0;
  if (lease > now) {
    if (session.page_visibility === "HIDDEN") {
      return { state: "Conectat · ecran în fundal", detail: `Ultimul semnal ${formatClock(session.client_last_seen_at)}`, tone: "warning", mark: "warn" };
    }
    return { state: "Conectat", detail: `Ultimul semnal ${formatClock(session.client_last_seen_at)}`, tone: "safe", mark: "on" };
  }
  return { state: "Fără semnal", detail: `Ultimul semnal ${formatClock(session.client_last_seen_at)}`, tone: "warning", mark: "warn" };
}

/** The assistant in her earbuds, as the phone reports it. */
export function voiceSignal(session: SessionView | null, _voiceMode?: unknown): Signal {
  if (!session || session.state === "CANCELLED") {
    return { state: "Fără sesiune", detail: "", tone: "neutral", mark: "unknown" };
  }
  if (session.operator_connected_at) {
    return { state: "Preluat de dispecer", detail: `De la ${formatClock(session.operator_connected_at)}`, tone: "safe", mark: "on" };
  }
  const issues = issuesOfKind(session, "voice");
  if (issues.length > 0) {
    return { state: issueLabel(issues[0]), detail: "", tone: "danger", mark: "off" };
  }
  if (session.audio_status === "CONNECTED") {
    return { state: "Activ", detail: "Asistentul vorbește cu ea în căști.", tone: "safe", mark: "on" };
  }
  if (session.audio_status === "ERROR" || session.audio_status === "DISCONNECTED") {
    return { state: "Fără sunet", detail: "Căștile sau microfonul nu răspund.", tone: "warning", mark: "warn" };
  }
  return { state: "Inactiv", detail: "Microfonul telefonului nu este pornit.", tone: "neutral", mark: "unknown" };
}

/** GPS as reported by the phone. */
export function gpsSignal(session: SessionView | null): Signal {
  if (!session || session.state === "CANCELLED") {
    return { state: "Fără sesiune", detail: "", tone: "neutral", mark: "unknown" };
  }
  if (session.gps_status === "GPS_AVAILABLE") {
    return { state: "Disponibil", detail: "Poziție recentă de la telefon.", tone: "safe", mark: "on" };
  }
  if (session.gps_status === "GPS_STALE") {
    return { state: "Poziție veche", detail: "Telefonul nu a mai trimis o poziție nouă.", tone: "warning", mark: "warn" };
  }
  return { state: "Indisponibil", detail: "Telefonul nu a trimis poziția.", tone: "neutral", mark: "unknown" };
}

const powerCopy: Record<string, { state: string; tone: Tone; mark: Signal["mark"] }> = {
  POWER_OK: { state: "Normală", tone: "safe", mark: "on" },
  POWER_UNSTABLE: { state: "Instabilă", tone: "warning", mark: "warn" },
  LOW_VOLTAGE: { state: "Tensiune scăzută", tone: "warning", mark: "warn" },
};

/** Raspberry Pi power: the monitor's own report wins over the session field. No battery %. */
export function powerSignal(session: SessionView | null, system: SystemPower | null): Signal {
  const issues = issuesOfKind(session, "power");
  if (issues.length > 0) {
    return { state: issueLabel(issues[0]), detail: "", tone: "warning", mark: "warn" };
  }
  const reported = system?.state ?? (session && session.power_status !== "POWER_STATUS_UNKNOWN" ? session.power_status : null);
  const copy = reported ? powerCopy[reported] : undefined;
  if (copy) {
    return {
      ...copy,
      detail: system ? `Verificat ${formatClock(system.at)}` : "",
    };
  }
  return { state: "Necunoscută", detail: "", tone: "neutral", mark: "unknown" };
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

