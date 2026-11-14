import { Copy, MessageSquare, Send, Smartphone, Trash2, UserPlus, Users } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import type { AccessGrant } from "../api/accessStore";
import { api, ApiError } from "../api/client";
import type { CompanionProfile, ContactInvite } from "../api/contract";
import { formatClock } from "../state/presentation";

/*
 * The person decides who is warned. Contacts are added here, on her own phone; the dispatcher
 * never chooses them. Each contact gets a one-time invite that her phone sends by SMS (her own
 * messaging app opens with the text ready; she presses Send). The contact opens the link and
 * allows notifications; from then on alerts reach that phone.
 */

function inviteText(invite: ContactInvite, ownerName: string | null): string {
  const link = `${window.location.origin}/contact#cod=${invite.code}`;
  const who = ownerName?.trim() || "Te-am";
  const lead = ownerName?.trim() ? `${who} te-a adăugat` : "Te-am adăugat";
  return `${lead} ca persoană de contact de urgență în Vesta. Dacă e nevoie, primești o alertă pe telefon. Deschide linkul și apasă „Permite notificările”: ${link} (cod ${invite.code}, valabil 24 h)`;
}

/** sms: link understood by Android messaging apps. */
function smsHref(phone: string, body: string): string {
  return `sms:${encodeURIComponent(phone)}?body=${encodeURIComponent(body)}`;
}

function contactState(contact: CompanionProfile["contacts"][number]): { label: string; tone: string } {
  if (contact.push_enabled) return { label: "Primește alerte", tone: "safe" };
  if (contact.accepted_at) return { label: "A acceptat · notificări oprite", tone: "warning" };
  if (contact.invite_pending) return { label: "Invitație în așteptare", tone: "neutral" };
  return { label: "Invitație expirată", tone: "warning" };
}

export function EmergencyContacts({ grant, onGrantLost }: { grant: AccessGrant; onGrantLost: () => void }) {
  const [profile, setProfile] = useState<CompanionProfile | null>(null);
  const [name, setName] = useState("");
  const [nameDraft, setNameDraft] = useState<string | null>(null);
  const [number, setNumber] = useState("");
  const [invite, setInvite] = useState<ContactInvite | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const handle = useCallback(
    (error: unknown, fallback: string) => {
      if (error instanceof ApiError && error.kind === "unauthorized") {
        onGrantLost();
        return;
      }
      if (error instanceof ApiError && error.code === "CONTACT_EXISTS") setProblem("Acest număr este deja contact de urgență.");
      else if (error instanceof ApiError && error.code === "TOO_MANY_CONTACTS") setProblem("Ai atins numărul maxim de contacte.");
      else if (error instanceof ApiError && error.kind === "invalid") setProblem("Verifică numele și numărul de telefon.");
      else setProblem(fallback);
    },
    [onGrantLost],
  );

  const load = useCallback(async () => {
    try {
      setProfile(await api.companionProfile(grant.token));
    } catch (error) {
      handle(error, "Lista de contacte nu a putut fi încărcată.");
    }
  }, [grant.token, handle]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(load, 15_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const ownerName = nameDraft ?? profile?.display_name ?? "";

  const saveName = async () => {
    if (nameDraft === null) return;
    try {
      setProfile(await api.updateProfile(grant.token, nameDraft));
      setNameDraft(null);
    } catch (error) {
      handle(error, "Numele nu a putut fi salvat.");
    }
  };

  const add = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      if (nameDraft !== null) await saveName();
      const created = await api.addContact(grant.token, name, number);
      setInvite(created);
      setName("");
      setNumber("");
      await load();
    } catch (error) {
      handle(error, "Contactul nu a putut fi adăugat.");
    } finally {
      setBusy(false);
    }
  };

  const reinvite = async (contactId: string) => {
    setProblem(null);
    try {
      setInvite(await api.reinviteContact(grant.token, contactId));
      await load();
    } catch (error) {
      handle(error, "Invitația nu a putut fi creată.");
    }
  };

  const remove = async (contactId: string) => {
    try {
      setProfile(await api.revokeContact(grant.token, contactId));
      if (invite?.contact.id === contactId) setInvite(null);
    } catch (error) {
      handle(error, "Contactul nu a putut fi eliminat.");
    }
  };

  const revokePhone = async (phoneId: string) => {
    try {
      const next = await api.revokePhone(grant.token, phoneId);
      if (phoneId === profile?.this_phone_id) onGrantLost();
      else setProfile(next);
    } catch (error) {
      handle(error, "Telefonul nu a putut fi eliminat.");
    }
  };

  const copyInvite = async () => {
    if (!invite) return;
    try {
      await navigator.clipboard.writeText(inviteText(invite, ownerName));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2500);
    } catch {
      setProblem("Mesajul nu a putut fi copiat. Folosește butonul SMS.");
    }
  };

  return (
    <section className="c-section c-card" aria-labelledby="contacts-head" data-testid="emergency-contacts">
      <h2 id="contacts-head" className="c-head">
        <Users size={18} aria-hidden="true" /> Contacte de urgență
      </h2>
      <p className="c-text">
        Tu alegi cine primește alerta. Contactul primește de la tine un SMS cu un link; după ce îl deschide și permite
        notificările, este anunțat pe telefon dacă Vesta emite o alertă.
      </p>

      <label className="field">
        <span>Numele tău, așa cum îl văd contactele</span>
        <span className="field-row">
          <input
            type="text"
            maxLength={60}
            value={ownerName}
            placeholder="de exemplu Ana"
            onChange={(event) => setNameDraft(event.target.value)}
            data-testid="owner-name"
          />
          {nameDraft !== null && (
            <button type="button" className="btn btn--outline btn--sm" onClick={() => void saveName()}>
              Salvează
            </button>
          )}
        </span>
      </label>

      {problem && <p className="notice notice--warning" role="alert">{problem}</p>}

      {invite && (
        <div className="invite-card" role="status" data-testid="invite-card">
          <p className="invite-title">
            <MessageSquare size={18} aria-hidden="true" /> Invitația pentru <strong>{invite.contact.name}</strong> este gata
          </p>
          <p className="invite-code num" aria-label="Cod de invitație">{invite.code.slice(0, 4)} {invite.code.slice(4)}</p>
          <p className="c-hint">
            Se folosește o singură dată, până{" "}
            {new Date(invite.expires_at).toLocaleString("ro-RO", { weekday: "long", hour: "2-digit", minute: "2-digit" })}.
          </p>
          <div className="btn-row">
            <a className="btn btn--primary" href={smsHref(invite.contact.phone, inviteText(invite, ownerName))} data-testid="invite-sms">
              <Send size={17} aria-hidden="true" /> Trimite prin SMS
            </a>
            <button type="button" className="btn btn--ghost" onClick={() => void copyInvite()}>
              <Copy size={17} aria-hidden="true" /> {copied ? "Copiat" : "Copiază mesajul"}
            </button>
          </div>
        </div>
      )}

      {profile && profile.contacts.length > 0 && (
        <ul className="contact-list" aria-label="Contacte de urgență">
          {profile.contacts.map((contact) => {
            const status = contactState(contact);
            return (
              <li key={contact.id} className="contact-item" data-testid="contact-item">
                <span className="contact-avatar" aria-hidden="true">{contact.name.slice(0, 1).toUpperCase()}</span>
                <span className="contact-text">
                  <strong>{contact.name}</strong>
                  <span className="num">{contact.phone}</span>
                  <span className={`contact-status tone--${status.tone}`}>
                    <span className="tone-dot" aria-hidden="true" />
                    {status.label}
                  </span>
                </span>
                <span className="contact-actions">
                  {!contact.push_enabled && (
                    <button type="button" className="icon-button" onClick={() => void reinvite(contact.id)} aria-label={`Invitație nouă pentru ${contact.name}`} title="Invitație nouă">
                      <Send size={18} />
                    </button>
                  )}
                  <button type="button" className="icon-button icon-button--danger" onClick={() => void remove(contact.id)} aria-label={`Elimină ${contact.name}`} title="Elimină">
                    <Trash2 size={18} />
                  </button>
                </span>
              </li>
            );
          })}
        </ul>
      )}

      <form className="contact-form" onSubmit={add}>
        <label className="field">
          <span>Nume contact</span>
          <input type="text" maxLength={60} value={name} onChange={(event) => setName(event.target.value)} autoComplete="off" data-testid="contact-name" />
        </label>
        <label className="field">
          <span>Număr de telefon</span>
          <input type="tel" inputMode="tel" maxLength={25} value={number} onChange={(event) => setNumber(event.target.value)} placeholder="+40 7…" autoComplete="off" data-testid="contact-phone" />
        </label>
        <button type="submit" className="btn btn--primary" disabled={busy || !name.trim() || number.replace(/\D/g, "").length < 6}>
          <UserPlus size={18} aria-hidden="true" /> {busy ? "Se adaugă…" : "Adaugă și pregătește invitația"}
        </button>
      </form>

      {profile && (
        <details className="phones">
          <summary>
            <Smartphone size={16} aria-hidden="true" /> Telefoane asociate cu dispozitivul ({profile.phones.length})
          </summary>
          <ul className="contact-list">
            {profile.phones.map((phone) => (
              <li key={phone.id} className="contact-item">
                <span className="contact-avatar" aria-hidden="true"><Smartphone size={16} /></span>
                <span className="contact-text">
                  <strong>{phone.label}{phone.id === profile.this_phone_id ? " · acesta" : ""}</strong>
                  <span>Văzut ultima dată la {formatClock(phone.last_seen_at)}</span>
                </span>
                <button type="button" className="icon-button icon-button--danger" onClick={() => void revokePhone(phone.id)} aria-label={`Elimină ${phone.label}`} title="Elimină asocierea">
                  <Trash2 size={18} />
                </button>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
