# Vesta — stratul de date (Tiger Data / TimescaleDB)

Cod: `backend/app/db/` (persistență) și `backend/app/integrations/` (outbox, heartbeat, alimentare, restart).
Driver: `pg8000` (pur Python; `psycopg` e blocat de Application Control pe laptopurile Windows).

## Rulare

```bash
pip install -r requirements.txt
cd backend && python -m app.db.migrate --status   # ce migrări sunt aplicate
cd backend && python -m app.db.migrate            # aplică ce lipsește (idempotent, cu advisory lock)
python scripts/tiger_smoke_test.py --cleanup      # scenariu demo complet
python -m pytest                                  # unit + integrare (schemă temporară vesta_test_*)
```

`TIMESCALE_SERVICE_URL` se citește din `.env` (ignorat de git).

## Tabele

| Tabel | Tip | Rol |
|---|---|---|
| `sessions` | tabel | o sesiune per activare validă; `state_version` crește la fiecare schimbare de stare |
| `session_events` | hypertable | istoric cronologic al sesiunii |
| `safety_checkins` | tabel | verificări: REQUESTED / DELIVERED / AUDIO_UNAVAILABLE / CONFIRMED / TIMED_OUT / CANCELLED |
| `alerts` | tabel | ACTIVE / RESOLVED; o singură alertă activă per sesiune |
| `notification_attempts` | tabel (outbox) | PENDING → SENDING → SENT → DELIVERED → VIEWED; FAILED / GAVE_UP |
| `locations` | hypertable | GPS de la /companion; retenție 7 zile |
| `technical_events` | hypertable | telefon, pagină, audio, ElevenLabs, GPS, alimentare, server; retenție 30 zile |
| `companion_clients` | tabel | starea curentă a fiecărui telefon (heartbeaturile nu se salvează individual) |
| `access_tokens` | tabel | doar SHA-256 al tokenului; roluri USER / CONTACT / OPERATOR / PUBLIC_DEMO |
| `ingested_events` | tabel | deduplicare globală după `event_id` |
| `session_events_daily` | agregat continuu | statistici zilnice pe tip de eveniment |
| `legacy_*` | — | datele vechi cu senzori ESP32, păstrate neatinse |

## Garanții oferite de baza de date

- **Activare idempotentă:** `sessions.activation_event_id` UNIQUE. Răspunsurile posibile sunt `CREATED`, `DUPLICATE_EVENT` și `ALREADY_ACTIVE`.
- **O sesiune deschisă per owner:** index unic parțial pe sesiunile în ACTIVE, CHECK_IN, UNCONFIRMED sau ALERT. Sesiunile încheiate nu sunt afectate.
- **O alertă activă per sesiune:** index unic parțial. Dacă SOS și timeout vin simultan, rezultă o singură alertă cu `triggers = {SOS_EXPLICIT, CHECKIN_TIMEOUT}` și o singură notificare.
- **RESOLVED e definitiv:** un trigger refuză redeschiderea alertei. O sesiune CANCELLED sau INTERRUPTED nu mai schimbă starea.
- **Confirmări validate:** se acceptă doar pe checkin-ul deschis al aceleiași sesiuni, înainte de `expires_at`, cât timp sesiunea nu e în ALERT. Altfel rezultatul e `LATE`, `NOT_ALLOWED`, `WRONG_SESSION` sau `NO_OPEN_CHECKIN`, iar confirmarea doar se înregistrează.
- **Cancel refuzat cu alertă activă:** anularea nu ocolește o alertă activă; aceasta se închide explicit cu `alerts.resolve()`.
- **Restart (`integrations/startup.recover_on_startup`):**
  - ACTIVE, CHECK_IN și UNCONFIRMED devin INTERRUPTED.
  - ALERT rămâne ALERT.
  - Notificările SENDING se reiau, iar cele SENT, DELIVERED și VIEWED nu se retrimit.
  - Nu se creează sesiuni noi. E idempotent.

## Contract cu ceilalți agenți (de confirmat)

**Backend (Agent 2):**
- Monitorul GPIO generează `activation_event_id` (UUID) o singură dată, când se atinge pragul de 5 s, și apelează `db.sessions.activate(owner_id, event_id)`. Apăsările sub 5 s nu ajung în DB.
- Safety Engine:
  - apelează `checkins.request` / `mark_delivered` / `mark_audio_unavailable` / `confirm`;
  - rulează periodic `checkins.expire_due()`;
  - face tranzițiile cu `sessions.transition(..., expected_version=...)`.
- Alertele se ridică prin `alerts.raise_alert(...)`, care trece singur sesiunea în ALERT.
- **De stabilit:** starea `INTERRUPTED` e nouă față de cele 6 stări din brief.
- **De stabilit:** la restart, o sesiune UNCONFIRMED devine INTERRUPTED. Alternativa ar fi escaladarea ei; decizia aparține Safety Engine.
- API-ul verifică fiecare cerere cu `db.tokens.authorize(token, session_id, action)`. Acțiunile sunt `control`, `read_location`, `read_history` și `ack_notification`.
- La pornire se apelează o singură dată `recover_on_startup(db)`, în procesul care deține monitorul GPIO.

**Frontend (Agent 1):**
- Heartbeat la 15 s: `client_id`, `session_id`, `visibility` (VISIBLE/HIDDEN). Se trimite și la `visibilitychange`.
- Fiecare cerere SOS, anulare sau confirmare are un `event_id` (UUID) generat o singură dată și refolosit la retrimitere.
- Tokenul se trimite în header `Authorization`, niciodată în URL.
- Pagina contactului trimite ack pentru DELIVERED și VIEWED.

**Integrare:** `OutboxDispatcher(db, sender)`. `sender` e transportul către pagina contactului (WebSocket) și nu e încă implementat.

## Limitări

- Fără heartbeat, serverul vede telefonul RECONNECTING (după 30 s) și apoi DISCONNECTED (după 45 s). Asta nu declanșează alertă. Un browser Android cu ecranul blocat poate suspenda pagina, iar comportamentul trebuie verificat pe telefonul real.
- `vcgencmd get_throttled` arată doar dacă a existat under-voltage. Nu există un procent de baterie. Dacă tensiunea scade suficient, Pi-ul se oprește și nicio notificare nu mai e garantată.
- Retenția de 7 zile pentru locații șterge și datele demo. Acestea se regenerează cu scriptul de smoke test.
- Pe hypertable, cheia unică include coloana de timp. De aceea deduplicarea pe `event_id` se face prin `ingested_events`.
