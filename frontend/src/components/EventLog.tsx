import type { LiveState } from "../state/liveState";
import { formatClock, issueLabel } from "../state/presentation";

/*
 * The dispatcher's journal, written for anyone: what happened, in one plain sentence, with a short
 * explanation. Technical noise (phone heartbeats, every 5-second position, Vesta's own lines that
 * are in the transcript) is not listed.
 */

type Kind = "system" | "check" | "warning" | "alert" | "success";
type Line = { title: string; detail?: string; kind: Kind };

const str = (value: unknown) => (typeof value === "string" ? value : undefined);

export function describeEvent(type: string, data: Record<string, unknown>): Line | null {
  const reason = str(data.reason);
  const source = str(data.source);
  switch (type) {
    case "session.activated":
      return { kind: "system", title: "Vesta a pornit", detail: "Butonul a fost ținut apăsat 5 secunde. Vesta o întreabă în căști dacă a fost o greșeală." };
    case "session.already_active":
      return { kind: "system", title: "Butonul a fost apăsat din nou", detail: "Vesta era deja pornită, așa că nu s-a deschis o sesiune nouă." };
    case "session.recovered":
      return { kind: "warning", title: "Sesiune reluată", detail: "Serverul a repornit și a continuat sesiunea în curs." };
    case "safety.check_in_requested":
      return typeof data.attempt === "number" && data.attempt >= 2
        ? { kind: "warning", title: "Vesta o întreabă din nou dacă e bine", detail: "Prima întrebare a rămas fără răspuns." }
        : { kind: "check", title: "Vesta o întreabă dacă e bine", detail: "Așteaptă răspunsul ei, vocal sau pe telefon." };
    case "safety.check_in_delivered":
      return {
        kind: "check",
        title: str(data.channel) === "voice" ? "Întrebarea s-a auzit în căști" : "Întrebarea a apărut pe ecranul telefonului",
      };
    case "safety.check_in_undelivered":
      return { kind: "warning", title: "Întrebarea nu a ajuns la ea", detail: "Telefonul sau căștile nu au răspuns. Vesta mai încearcă." };
    case "safety.check_in_unconfirmed":
      return { kind: "warning", title: "Nu a răspuns la întrebare", detail: "Dacă nu răspunde nici a doua oară, se trimite alerta." };
    case "safety.check_in_confirmed":
      return { kind: "success", title: "A spus că este bine", detail: source === "voice_agent" ? "Răspuns vocal, înțeles de asistent." : "Răspuns de pe telefon." };
    case "safety.check_in_deferred":
      return { kind: "warning", title: "Întrebare amânată", detail: "O problemă la telefon sau la căști împiedică întrebarea. Se reîncearcă singur." };
    case "safety.alerted":
      if (reason === "MISSED_CHECK_INS") {
        return { kind: "alert", title: "ALERTĂ: nu a răspuns la întrebări", detail: "Dispeceratul și persoana de încredere au fost anunțați." };
      }
      return {
        kind: "alert",
        title: source === "voice_agent" ? "ALERTĂ: a cerut ajutor vocal" : "ALERTĂ: a cerut ajutor",
        detail: "Dispeceratul și persoana de încredere au fost anunțați.",
      };
    case "alert.notification_requested":
      return {
        kind: "alert",
        title: "Persoana de încredere a fost anunțată",
        detail: data.location_available ? "Împreună cu locația ei." : "Locația apare pe hartă imediat ce o trimite telefonul.",
      };
    case "alert.viewed":
      return { kind: "success", title: "Persoana de încredere a văzut alerta" };
    case "conversation.updated": {
      if (str(data.role) !== "person") return null; // Vesta's lines are in the transcript
      const action = str(data.action);
      return {
        kind: action === "ESCALATE" ? "alert" : "check",
        title: "Ea i-a răspuns lui Vesta",
        detail:
          action === "ESCALATE"
            ? "A cerut ajutor."
            : action === "FALSE_ALARM"
              ? "A spus că a fost o greșeală."
              : action === "CONFIRM_SAFE"
                ? "A spus că este bine."
                : "Vezi transcrierea apelului.",
      };
    }
    case "alert.resolved":
      return str(data.resolution_action) === "FALSE_ALARM"
        ? { kind: "success", title: "Alarmă falsă: alerta a fost oprită", detail: "Ea a spus că a fost o greșeală." }
        : { kind: "success", title: "Alerta a fost închisă" };
    case "session.cancelled":
      if (reason === "FALSE_ALARM") {
        return {
          kind: "success",
          title: "Alarmă falsă: Vesta s-a oprit",
          detail: str(data.from_state) === "ALERT" ? "Alerta deja trimisă a fost oprită." : "Nimeni nu a fost anunțat.",
        };
      }
      if (reason === "ACCIDENTAL_ACTIVATION") return { kind: "system", title: "Pornire din greșeală, anulată" };
      return { kind: "system", title: "Vesta s-a oprit", detail: "Sesiunea s-a încheiat." };
    case "session.data_deleted":
      return { kind: "system", title: "Datele sesiunii au fost șterse", detail: "Vocea, transcrierea și locația (alarmă falsă)." };
    case "technical.issue_reported":
      return { kind: "warning", title: `Problemă: ${issueLabel(str(data.issue) ?? "")}` };
    case "technical.issue_resolved":
      return { kind: "success", title: `Rezolvat: ${issueLabel(str(data.issue) ?? "")}` };
    case "technical.power":
      return { kind: "warning", title: "Alimentarea dispozitivului", detail: str(data.state) };
    case "system.recovered":
      return { kind: "warning", title: "Serverul a repornit" };
    // Not useful to a person reading the journal: shown elsewhere or too frequent.
    case "safety.alert_resolved":
    case "location.updated":
    case "client.status_changed":
    case "voice.status_changed":
    case "contact.notification":
      return null;
    default:
      return null;
  }
}

/** Events received from the server stream since this page opened, deduplicated by event_id. */
export function EventLog({ log, limit = 40 }: { log: LiveState["log"]; limit?: number }) {
  const rows = log
    .map((entry) => ({ entry, line: entry.type === "client.gap" ? null : "sequence" in entry ? describeEvent(entry.type, entry.data) : null }))
    .filter(({ entry, line }) => entry.type === "client.gap" || line !== null)
    .slice(0, limit);
  return (
    <ol className="timeline" aria-label="Evenimente">
      {rows.map(({ entry, line }) =>
        entry.type === "client.gap" ? (
          <li className="tl-row tl-row--gap" key={entry.id}>
            <time className="num">{formatClock(entry.occurredAt).slice(0, 5)}</time>
            <span className="tl-node" aria-hidden="true" />
            <span className="tl-copy">
              <strong>Legătura cu serverul s-a întrerupt câteva clipe</strong>
              <span>Starea de pe ecran a fost reîncărcată; unele evenimente din acel interval nu apar în listă.</span>
            </span>
          </li>
        ) : (
          line && (
            <li className={`tl-row tl-row--${line.kind}`} key={entry.id} data-testid="log-row">
              <time className="num" title={formatClock(entry.occurredAt)}>
                {formatClock(entry.occurredAt).slice(0, 5)}
              </time>
              <span className="tl-node" aria-hidden="true" />
              <span className="tl-copy">
                <strong>{line.title}</strong>
                {line.detail && <span>{line.detail}</span>}
              </span>
            </li>
          )
        ),
      )}
      <li className="tl-row tl-row--pending">
        <span className="tl-time-empty num" aria-hidden="true">--:--</span>
        <span className="tl-node" aria-hidden="true" />
        <span className="tl-copy">
          <span>
            {rows.length === 0 ? "Nimic deocamdată." : ""}
          </span>
        </span>
      </li>
    </ol>
  );
}
