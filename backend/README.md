# Vesta backend

Acest director conține strict nucleul atribuit rolului „Persoana 2”: serverul FastAPI,
mașina deterministă de stări, gateway-ul vocal ElevenLabs, autorizarea comenzilor de
sesiune și fluxul de evenimente WebSocket.

Vesta este un prototip demonstrativ de siguranță personală. Backendul nu apelează 112,
nu confirmă că a avut loc o agresiune și nu trebuie folosit ca serviciu de urgență real.

## Pornire rapidă

Este necesar Python 3.11 sau mai nou. Aceleași comenzi funcționează pe un laptop și pe
un Raspberry Pi cu Python compatibil; configurarea sistemului de operare și deploymentul
pe Raspberry Pi aparțin fluxului de integrare, nu acestui modul.

### PowerShell

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

$env:VESTA_CONTROL_SIGNING_SECRET = "înlocuiește-cu-minim-32-caractere-aleatoare"
$env:VESTA_VOICE_MODE = "demo"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### Linux / Raspberry Pi OS

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

export VESTA_CONTROL_SIGNING_SECRET='înlocuiește-cu-minim-32-caractere-aleatoare'
export VESTA_VOICE_MODE='demo'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Pentru acces din rețeaua locală se poate folosi `--host 0.0.0.0`, dar numai într-o rețea
de test controlată, cu `VESTA_DEVICE_API_KEY` setat și originile CORS restrânse. Aplicația
citește variabilele din mediul procesului; nu încarcă automat un fișier `.env`.
`backend/.env.example` este doar inventarul valorilor necesare.

În afara mediilor `development` și `test`, pornirea eșuează intenționat dacă lipsesc
`VESTA_CONTROL_SIGNING_SECRET` sau `VESTA_DEVICE_API_KEY`. Statusul și fluxul WebSocket
sunt read-only și nu au autentificare de utilizator în MVP, deci serverul nu trebuie expus
public; conexiunile WebSocket din browser sunt limitate la originile configurate.

După pornire:

- health check: `GET http://127.0.0.1:8000/health`;
- documentație interactivă: `http://127.0.0.1:8000/docs`;
- schema OpenAPI: `http://127.0.0.1:8000/openapi.json`.

## Configurare

| Variabilă | Implicit | Rol |
| --- | --- | --- |
| `VESTA_APP_NAME` | `Vesta Safety Backend` | Titlul aplicației și al documentației OpenAPI. |
| `VESTA_ENVIRONMENT` | `development` | Eticheta mediului de rulare. |
| `VESTA_CONTROL_SIGNING_SECRET` | secret efemer | Secret HMAC de minimum 32 bytes pentru tokenurile de control. Trebuie setat pentru ca tokenurile să rămână valide după restart. |
| `VESTA_CONTROL_TOKEN_TTL_SECONDS` | `14400` | Durata tokenului de control al sesiunii. |
| `VESTA_DEVICE_API_KEY` | neconfigurat | Dacă este setată, activarea ESP32 cere headerul `X-Device-Key`. |
| `VESTA_VOICE_MODE` | `elevenlabs` | `elevenlabs` pentru WebRTC live sau `demo` pentru răspunsuri scriptate offline. |
| `ELEVENLABS_API_KEY` | neconfigurat | Cheia permanentă folosită exclusiv server-side. |
| `ELEVENLABS_AGENT_ID` | neconfigurat | Agentul privat ElevenLabs folosit pentru conversație. |
| `ELEVENLABS_TIMEOUT_SECONDS` | `5` | Timeoutul cererii de credential temporar. |
| `ELEVENLABS_RETRY_COUNT` | `1` | Numărul de retry-uri; providerul acceptă doar `0` sau `1`. |
| `VESTA_CHECKIN_INTERVAL_SECONDS` | `60` | Timpul dintre activare/confirmare și următorul check-in. |
| `VESTA_CHECKIN_RESPONSE_TIMEOUT_SECONDS` | `15` | Fereastra de răspuns pentru o încercare. |
| `VESTA_CHECKIN_RETRY_DELAY_SECONDS` | `5` | Pauza până la noua încercare și replanificarea în caz de eroare tehnică. |
| `VESTA_CHECKIN_MAX_ATTEMPTS` | `2` | Numărul de încercări fără confirmare înainte de `ALERT`. |
| `VESTA_CLIENT_HEARTBEAT_TIMEOUT_SECONDS` | `20` | Lease-ul Android; expirarea lui devine problemă tehnică de rețea, nu alertă. |
| `VESTA_SCHEDULER_TICK_SECONDS` | `0.25` | Frecvența schedulerului intern. |
| `VESTA_SCHEDULER_ENABLED` | `true` | Activează tranzițiile temporizate automate. |
| `VESTA_CORS_ORIGINS` | originile Vite locale | Listă separată prin virgulă; wildcardul `*` este respins. |
| `VESTA_WEBSOCKET_QUEUE_SIZE` | `256` | Capacitatea cozii fiecărui client WebSocket. |
| `VESTA_WEBSOCKET_HEARTBEAT_SECONDS` | `15` | Intervalul heartbeat-ului WebSocket. |

Pentru modul live:

```powershell
$env:VESTA_VOICE_MODE = "elevenlabs"
$env:ELEVENLABS_API_KEY = "..."
$env:ELEVENLABS_AGENT_ID = "..."
```

Cheia ElevenLabs nu apare în răspunsurile API, tokenurile de control, schema OpenAPI sau
mesajele de eroare ale providerului.

## Modelul de autorizare și idempotency

`POST /api/v1/device/events` este protejat cu `X-Device-Key` numai când
`VESTA_DEVICE_API_KEY` este configurat. O activare reușită returnează un
`control_token` scoped pe sesiune și data lui de expirare. Toate comenzile ulterioare ale
sesiunii cer:

```http
Authorization: Bearer <control_token>
```

Tokenul nu reprezintă identitatea unei persoane; autorizează doar comenzile pentru
sesiunea din claim. Fiecare comandă conține un `event_id`. Retrimiterea aceleiași comenzi
este idempotentă (`duplicate: true`), iar reutilizarea aceluiași `event_id` pentru altă
comandă produce `409 EVENT_ID_REUSED`. MVP-ul păstrează în memorie o fereastră LRU cu
ultimele 10.000 de ID-uri de comandă.

## API

### Sistem și activare

| Metodă | Rută | Autorizare | Scop |
| --- | --- | --- | --- |
| `GET` | `/health` | - | Verifică procesul și modul vocal configurat. |
| `GET` | `/api/v1/status` | - | Returnează starea agregată și sesiunile din memorie. |
| `POST` | `/api/v1/device/events` | `X-Device-Key` dacă este configurat | Procesează evenimentul ESP32 `activate`. Răspunde `201` la creare și `200` la duplicat. |

Activare:

```json
{
  "event_id": "evt-001",
  "device_id": "vesta-01",
  "event": "activate"
}
```

Câmpul opțional `occurred_at` este acceptat pentru contractul dispozitivului, dar timpul
tranziției este stabilit de server.

### Comenzi de sesiune

| Metodă | Rută | Corp minim | Efect |
| --- | --- | --- | --- |
| `POST` | `/api/v1/sessions/{session_id}/checkins` | `{"event_id":"...","confirmation":"safe","source":"android"}` | Confirmă explicit starea de bine din `CHECK_IN` sau `UNCONFIRMED`. |
| `POST` | `/api/v1/sessions/{session_id}/sos` | `{"event_id":"...","source":"android"}` | Trece imediat în `ALERT`, independent de ElevenLabs. `reason` este opțional. |
| `POST` | `/api/v1/sessions/{session_id}/cancel` | `{"event_id":"..."}` | Anulează autorizat o sesiune activă sau aflată în alertă. `reason` este opțional. |
| `POST` | `/api/v1/sessions/{session_id}/heartbeat` | `{}` | Reînnoiește lease-ul autentificat al conexiunii Android. |
| `POST` | `/api/v1/sessions/{session_id}/technical-status` | `{"event_id":"...","issue":"microphone_denied","active":true}` | Raportează sau rezolvă separat o problemă tehnică. |
| `POST` | `/api/v1/sessions/{session_id}/voice/access` | `{"transport":"webrtc"}` | Emite credentialul temporar ElevenLabs sau configurația scriptată demo. |

Valorile acceptate pentru `source` sunt documentate în OpenAPI. Problemele tehnice sunt
`network_disconnected`, `voice_unavailable`, `microphone_denied` și
`audio_interrupted`; `active: false` marchează rezolvarea lor.

După pornirea interfeței audio, Android trebuie să trimită periodic `POST .../heartbeat`.
Primul heartbeat activează monitorizarea conexiunii pentru sesiune. Dacă lease-ul expiră,
backendul adaugă automat `network_disconnected` înainte de evaluarea timeoutului și amână
escaladarea. Primul heartbeat de după reconectare rezolvă problema tehnică.

Răspunsul live al rutei de voce conține un `conversation_token` temporar și
`conversation_id`. Răspunsul demo este marcat explicit cu `provider: "demo"`,
`mode: "demo"`, `transport: "scripted"` și include replici simulate în `client_config`.

## Mașina de stări

Stările sunt `IDLE`, `ACTIVE`, `CHECK_IN`, `UNCONFIRMED`, `ALERT` și `CANCELLED`.
`IDLE` este starea sistemului când nu există sesiuni neterminate; o sesiune nouă este
activată direct în `ACTIVE`.

```text
IDLE -> ACTIVE -> CHECK_IN -> ACTIVE
                   |
                   v
              UNCONFIRMED -> CHECK_IN
                                  |
                                  v (după numărul configurat de timeouturi)
                                ALERT

ACTIVE / CHECK_IN / UNCONFIRMED --SOS--> ALERT
ACTIVE / CHECK_IN / UNCONFIRMED / ALERT --cancel autorizat--> CANCELLED
```

O confirmare validă resetează contorul de încercări și programează următorul check-in.
Problemele tehnice sunt ortogonale stării de siguranță: cât timp una este activă,
schedulerul amână timeoutul și nu produce automat `ALERT`. SOS-ul explicit rămâne
funcțional. Agentul vocal poate transmite acțiuni explicite, dar nu poate modifica direct
starea și nu decide dacă a avut loc o agresiune.

Pentru resetarea demo-ului, anulează autorizat sesiunea curentă (inclusiv din `ALERT`), apoi
trimite o activare nouă cu un `event_id` nou. Refolosirea vechiului `event_id` este tratată
ca retransmitere și returnează sesiunea inițială.

## WebSocket

`WS /ws/events` se abonează înainte de snapshot, apoi trimite `system.snapshot` cu un
watermark `sequence`/`latest_sequence` atomic și evenimente ordonate pentru
schimbările backendului:

- `session.activated`;
- `safety.check_in_requested`;
- `safety.check_in_unconfirmed`;
- `safety.check_in_confirmed`;
- `safety.check_in_deferred`;
- `safety.alerted`;
- `session.cancelled`;
- `technical.issue_reported`;
- `technical.issue_resolved`;
- `system.heartbeat`;
- `stream.resync_required` când un client lent pierde evenimente și trebuie să refacă
  snapshotul prin `GET /api/v1/status`.

Fiecare mesaj conține `event_id`, `sequence`, `type`, `occurred_at`, `session_id`,
`device_id` și `data`. Un client WebSocket deconectat sau lent nu poate bloca tranzițiile
de siguranță.

## Erori

Erorile aplicației au forma:

```json
{
  "error": {
    "code": "INVALID_SESSION_TRANSITION",
    "message": "...",
    "details": {}
  }
}
```

Codurile HTTP uzuale sunt `401` pentru token/device key invalid, `404` pentru sesiune
inexistentă, `409` pentru conflict de stare sau idempotency, `422` pentru payload invalid
și `503` pentru indisponibilitatea configurării/providerului vocal. Un eșec vocal este
publicat ca problemă tehnică și nu declanșează singur o alertă.

## Testare

```bash
cd backend
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m compileall -q app tests
```

Testele live cu ElevenLabs trebuie rulate separat, numai cu credențiale și dispozitive de
test autorizate:

```bash
RUN_ELEVENLABS_LIVE=1 python -m pytest -m live -s tests/live
```

Testul live verifică emiterea tokenului WebRTC și bugetul de latență al gateway-ului.
Calitatea audio, conversația și rutarea Bluetooth necesită telefonul și căștile reale.
Verificarea manuală Android este descrisă în
[`docs/ELEVENLABS.md`](docs/ELEVENLABS.md).

## Limite intenționate

- Starea, idempotency și istoricul WebSocket sunt păstrate doar în memorie și se pierd la
  restart. Persistența/Tiger Data aparțin rolului de bază de date și integrare.
- Nu sunt implementate telemetria senzorilor, locația, istoricul TimescaleDB, notificarea
  contactului de încredere, frontendul, aplicația Android, firmware-ul sau deploymentul.
- Backendul nu pornește microfonul Android, nu selectează căștile Bluetooth și nu
  proxiază audio prin Raspberry Pi.
- Nu există apeluri reale către 112 sau alte servicii de urgență.

