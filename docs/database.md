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
| `technical_events_hourly` | agregat continuu | sănătatea dispozitivelor IoT pe oră (telefon, GPS, alimentare, GPIO, audio) |
| `gps_quality_5m` | agregat continuu | calitatea senzorului GPS la 5 min (poziții, precizie, întârziere), fără coordonate |
| `conversation_turns`, `session_recordings`, `recording_chunks`, `operator_messages` | tabele | transcrierea și înregistrările unei alerte |
| `legacy_*` | — | date vechi ESP32; `legacy_sensor_readings` a fost scos de migrarea 007 |

## Compresie și analize live (migrarea 010)

- Columnstore pe toate cele trei hypertable-uri: `session_events` după 7 zile (segment `event_type`),
  `technical_events` după 2 zile (segment `component`), `locations` după 1 zi (segment `source`).
  Măsurat pe 3 zile de evenimente tehnice: 29,2 MB → 2,8 MB (90,4% economisit), interogabile cu SQL simplu.
- Agregatele continue au `materialized_only = false`: un eveniment nou apare imediat în dashboard.
- `GET /api/v1/analytics` (cheia dashboard) întoarce: starea live, timpii alertă → contact (p50/p95),
  statistici zilnice, sănătatea dispozitivelor, calitatea GPS și mărimea/compresia fiecărui hypertable.
  Panoul „Analize live” din dashboard îl citește la 10 s. Fără `VESTA_PERSISTENCE=tiger` panoul nu apare.
- `backend/start-lan.ps1` pornește în modul `tiger` dacă `TIMESCALE_SERVICE_URL` e în `.state/ai-keys.env`
  sau în `.env`-ul de la rădăcină.

## Garanții oferite de baza de date

- **Activare idempotentă:** `sessions.activation_event_id` UNIQUE. Răspunsurile posibile sunt `CREATED`, `DUPLICATE_EVENT` și `ALREADY_ACTIVE`.
- **O sesiune deschisă per owner:** index unic parțial pe sesiunile în ACTIVE, CHECK_IN, UNCONFIRMED sau ALERT. Sesiunile încheiate nu sunt afectate.
- **O alertă activă per sesiune:** index unic parțial. Dacă SOS și timeout vin simultan, rezultă o singură alertă cu `triggers = {SOS_EXPLICIT, CHECKIN_TIMEOUT}` și o singură notificare.
- **RESOLVED e definitiv:** un trigger refuză redeschiderea alertei. O sesiune CANCELLED sau INTERRUPTED nu mai schimbă starea.
- **Confirmări validate:** se acceptă doar pe checkin-ul deschis al aceleiași sesiuni, înainte de `expires_at`, cât timp sesiunea nu e în ALERT. Altfel rezultatul e `LATE`, `NOT_ALLOWED`, `WRONG_SESSION` sau `NO_OPEN_CHECKIN`, iar confirmarea doar se înregistrează.
- **Cancel refuzat cu alertă activă:** anularea nu ocolește o alertă activă; aceasta se închide explicit cu `alerts.resolve()`.
- **Restart (`integrations/startup.recover_on_startup`):**
  - Toate sesiunile deschise devin INTERRUPTED, pentru că SessionManager le ține în memorie și nu le poate continua.
  - Alertele ACTIVE rămân active în `alerts` până la o rezolvare explicită.
  - Notificările SENDING se reiau, iar cele SENT, DELIVERED și VIEWED nu se retrimit.
  - Nu se creează sesiuni noi. E idempotent.

## Legătura cu backendul (SessionManager)

Activată cu `VESTA_PERSISTENCE=tiger`. SessionManager rămâne autoritativ (stare în memorie, Safety Engine, tokenuri HMAC). Fiecare tranziție publicată ajunge la `TigerRecorder` (`integrations/persistence.py`), care o scrie în fundal, în ordine.

| Tranziție (Safety Engine) | Ce se scrie |
|---|---|
| `activated` | `sessions.activate` (același `session_id`) |
| `check_in_started` | stare CHECK_IN + `safety_checkins` (rundă, încercare, timeout-ul engine-ului) |
| `check_in_missed` | checkin TIMED_OUT + stare UNCONFIRMED |
| `check_in_confirmed` | `checkins.confirm` (VOICE_TOOL / UI_BUTTON) + stare ACTIVE |
| `alerted` | `alerts.raise_alert` (SOS_EXPLICIT / CHECKIN_TIMEOUT) + notificări în outbox |
| `alert_resolved` | `alerts.resolve`: alerta devine RESOLVED, contactele primesc ALERT_RESOLVED |
| `cancelled` | `sessions.cancel` (`reason` devine `end_reason`, ex. ACCIDENTAL_ACTIVATION) |
| `technical_issue_*` | `technical_events` (PHONE_*, SERVICE_*, AUDIO_*) |

- Momentele evenimentelor din timeline vin din engine. Câmpurile operaționale ale bazei
  (`updated_at`, retry/outbox și închiderea la restart) folosesc ceasul Tiger Data.
- `event_id`-ul clientului (text) devine un UUID determinist, deci o comandă retrimisă nu se scrie de două ori.
- Dacă Tiger Data e indisponibil, SOS și verificările continuă, iar erorile apar în log (testat).

`integrations/runtime.TigerPersistence` pornește din lifespan-ul FastAPI:
- recuperarea la pornire;
- recorderul;
- outbox-ul, care publică `contact.notification` pe `/ws/events`, fără coordonate;
- monitorul de alimentare (`technical.power`).

## Contract cu celelalte componente

**Backend (Agent 2):**
- Monitorul GPIO generează `activation_event_id` o singură dată când se atinge pragul de 5 s și
  activează `SessionManager`. Apăsările sub 5 s nu produc o tranziție și nu ajung în DB.
- Safety Engine rămâne autoritativ pentru starea live. `TigerRecorder` consumă în ordine
  tranzițiile publicate și apelează repository-urile `sessions`, `checkins`, `alerts` și `events`;
  o eroare de rețea nu blochează SOS-ul sau temporizările de siguranță.
- Generarea, livrarea, indisponibilitatea audio și confirmarea unui check-in sunt tranziții
  distincte și păstrează același `checkin_id` în engine și în baza de date.
- Rezolvarea unei alerte este o comandă autorizată explicită; anularea obișnuită nu închide o
  alertă deja transmisă.
- `INTERRUPTED` apare doar în istoric. Starea live după restart e `IDLE` (descris în `shared/API_CONTRACT.md`).
- Controlul sesiunii rămâne pe tokenurile HMAC ale backendului. `db.tokens` (roluri CONTACT/OPERATOR/PUBLIC_DEMO) e pentru viitoarele endpointuri de citire (locație, istoric), care nu există încă.
- `recover_on_startup` rulează automat din lifespan; uvicorn trebuie pornit cu un singur worker.

**Frontend (Agent 1):**
- Heartbeat la 15 s: `client_id`, `session_id`, `visibility` (VISIBLE/HIDDEN). Se trimite și la `visibilitychange`.
- Fiecare cerere SOS, anulare sau confirmare are un `event_id` (UUID) generat o singură dată și refolosit la retrimitere.
- Tokenul se trimite în header `Authorization`, niciodată în URL.
- Pagina contactului trimite ack pentru DELIVERED și VIEWED.

**Integrare:** `TigerPersistence` injectează în `OutboxDispatcher` transportul care publică
`contact.notification` pe fluxul WebSocket autentificat. Evenimentul nu conține coordonate;
ack-urile `DELIVERED`/`VIEWED` rămân responsabilitatea paginii contactului.

## Limitări

- Fără heartbeat, serverul vede telefonul RECONNECTING (după 30 s) și apoi DISCONNECTED (după 45 s). Asta nu declanșează alertă. Un browser Android cu ecranul blocat poate suspenda pagina, iar comportamentul trebuie verificat pe telefonul real.
- `vcgencmd get_throttled` arată doar dacă a existat under-voltage. Nu există un procent de baterie. Dacă tensiunea scade suficient, Pi-ul se oprește și nicio notificare nu mai e garantată.
- Retenția de 7 zile pentru locații șterge și datele demo. Acestea se regenerează cu scriptul de smoke test.
- Pe hypertable, cheia unică include coloana de timp. De aceea deduplicarea pe `event_id` se face prin `ingested_events`.
