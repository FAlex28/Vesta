# Vesta backend — Agent 2

Acest director conține exclusiv componentele rolului „VESTA — AGENT 2: BACKEND, AI ȘI
SAFETY ENGINE”: serverul FastAPI, motorul determinist de siguranță, integrarea vocală
ElevenLabs, monitorizarea joystickului conectat direct la Raspberry Pi și contractele de
comunicare aferente.

Vesta este un prototip demonstrativ. Backendul nu apelează 112, nu deduce că a avut loc o
agresiune și nu trebuie tratat ca serviciu real de urgență.

## Pornire

Este necesar Python 3.11 sau mai nou.

### Laptop / CI (GPIO simulat)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

$env:VESTA_CONTROL_SIGNING_SECRET = "înlocuiește-cu-minim-32-caractere-aleatoare"
$env:VESTA_GPIO_MODE = "mock"
$env:VESTA_VOICE_MODE = "demo"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

### Raspberry Pi (GPIO real)

Conectare joystick: `SW → GPIO17` (pin fizic 11), `GND → GND` (pin fizic 6).

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,raspberry]'

export VESTA_CONTROL_SIGNING_SECRET='înlocuiește-cu-minim-32-caractere-aleatoare'
export VESTA_ENVIRONMENT='production'
export VESTA_DEVICE_API_KEY='înlocuiește-cu-secret-pentru-ruta-de-integrare'
export VESTA_DASHBOARD_API_KEY='înlocuiește-cu-secret-pentru-dashboard'
export VESTA_GPIO_MODE='gpio'
export VESTA_GPIO_PIN='17'
export VESTA_HOLD_SECONDS='5.0'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Monitorul folosește numerotare BCM, `pull_up=True`, debounce `0.08 s`, `when_held` și
`hold_repeat=False`. O apăsare continuă produce un singur eveniment după 5 secunde, fără a
aștepta eliberarea; o apăsare nouă este eligibilă numai după release. Dacă butonul este deja
apăsat la pornire, monitorul se armează abia după prima eliberare. Un lock de proces împiedică
mai multe instanțe să revendice simultan GPIO17. Callbackul GPIO pune doar un eveniment în
coada internă; I/O-ul extern se face în afara callbackului.

Procesul trebuie pornit cu exact `--workers 1`. Sesiunile, registrul de idempotency și adaptoarele
implicite sunt în memorie, iar un singur proces trebuie să dețină monitorul GPIO. Pentru mai
multe instanțe ar fi necesară coordonare distribuită și un provider persistent comun, care nu
fac parte din acest rol.

Pentru acces din LAN, expunerea pe `0.0.0.0` se face numai într-o rețea de test controlată,
cu cheile configurate și originile CORS restrânse. Aplicația nu încarcă automat `.env`;
`.env.example` este inventarul configurației.

## Configurare

| Variabilă | Implicit | Rol |
| --- | --- | --- |
| `VESTA_ENVIRONMENT` | `development` | Mediul de rulare. În afara `development`/`test`, se cer secrete stabile. |
| `VESTA_CONTROL_SIGNING_SECRET` | secret efemer în dev | HMAC de minimum 32 bytes pentru tokenurile scoped pe sesiune. |
| `VESTA_CONTROL_TOKEN_TTL_SECONDS` | `14400` | Durata tokenului de control. |
| `VESTA_DEVICE_API_KEY` | neconfigurat | Protejează ruta de integrare `/device/events`; activarea GPIO internă nu o folosește. |
| `VESTA_DASHBOARD_API_KEY` | neconfigurat | Protejează statusul global și fluxul dashboard unde este configurat. |
| `VESTA_CAMERA_BASE_URL` | neconfigurat | Adresa serverului video de pe Pi; implicit pe Pi: `http://127.0.0.1:8080`. |
| `VESTA_CAMERA_API_KEY` | neconfigurat | Cheia server-side pentru proxy-ul securizat către camere. Obligatorie pentru adrese din rețea. |
| `VESTA_GPIO_MODE` | `mock` în dev/test | `gpio`, `mock` sau `disabled`; în afara dev/test este obligatoriu `gpio`. |
| `VESTA_GPIO_PIN` | `17` | Pin BCM al butonului. |
| `VESTA_HOLD_SECONDS` | `5.0` | Durata continuă necesară activării. |
| `VESTA_GPIO_DEBOUNCE_SECONDS` | `0.08` | Debounce hardware. |
| `VESTA_GPIO_DEVICE_ID` | `vesta-rpi-01` | Identificatorul sursei fizice. |
| `VESTA_GPIO_LOCK_PATH` | `/tmp/vesta-gpio17.lock` | Lock inter-proces pentru un singur monitor. |
| `VESTA_QUICK_CANCEL_SECONDS` | `5` | Fereastra de anulare accidentală după activare. |
| `VESTA_CHECKIN_INTERVAL_SECONDS` | `60` | Intervalul până la check-in. |
| `VESTA_CHECKIN_RESPONSE_TIMEOUT_SECONDS` | `15` | Fereastra de răspuns la check-in. |
| `VESTA_CHECKIN_RETRY_DELAY_SECONDS` | `5` | Pauza dintre încercări. |
| `VESTA_CHECKIN_MAX_ATTEMPTS` | `2` | Încercări fără răspuns înainte de alertă. |
| `VESTA_CLIENT_HEARTBEAT_TIMEOUT_SECONDS` | `45` | Lease-ul conexiunii clientului. |
| `VESTA_PERSISTENCE` | `memory` | `memory` pentru rulare locală/teste sau `tiger` pentru istoricul Tiger Data. |
| `TIMESCALE_SERVICE_URL` | neconfigurat | URL-ul PostgreSQL/Tiger Data, obligatoriu când persistența este `tiger`. |
| `VESTA_TRUSTED_CONTACT_IDS` | `demo-contact` | Lista separată prin virgulă a destinatarilor din outbox. |
| `VESTA_VOICE_MODE` | `elevenlabs` | `elevenlabs` live sau `demo` scriptat. |
| `ELEVENLABS_API_KEY` | neconfigurat | Secret permanent, folosit numai server-side. |
| `ELEVENLABS_AGENT_ID` | neconfigurat | Agentul privat ElevenLabs. |

## Reguli de siguranță

Stările sesiunii sunt `IDLE`, `ACTIVE`, `CHECK_IN`, `UNCONFIRMED`, `ALERT` și
`CANCELLED`. Starea tehnică este separată: `HEALTHY`, `DEGRADED` sau `DISCONNECTED`.
Fiecare modificare are o versiune monotonă pentru controlul concurenței.

```text
IDLE -> ACTIVE -> CHECK_IN -> ACTIVE
                   |            ^
                   v            |
              UNCONFIRMED ------+
                   |
                   v (după timeouturile configurate)
                 ALERT --rezolvare explicită--> CANCELLED

ACTIVE / CHECK_IN / UNCONFIRMED --SOS explicit--> ALERT
```

- O activare nouă în timp ce există o sesiune neterminată nu creează altă sesiune. Răspunsul
  semantic este `ALREADY_ACTIVE`, iar starea curentă este republicată.
- SOS-ul este independent de conversația vocală și de fereastra de anulare rapidă.
- `ACCIDENTAL_ACTIVATION` este acceptată numai în fereastra configurată; sesiunea rămâne
  activă până când comanda autorizată este validată.
- Un check-in are `checkin_id` unic. Generarea întrebării, livrarea audio și confirmarea sunt
  evenimente distincte. Confirmările validează sesiunea, check-in-ul activ și termenul; un
  răspuns întârziat nu modifică o alertă deja emisă.
- O alertă are un singur `alert_id` activ pe sesiune. SOS și timeout concurente sunt deduplicate,
  iar notificarea este solicitată o singură dată. Închiderea păstrează alerta ca `RESOLVED`, cu
  timestamp și acțiune; nu o șterge.
- GPS-ul este opțional (`GPS_AVAILABLE`, `GPS_STALE`, `GPS_UNAVAILABLE`). Backendul nu inventează
  coordonate și nu blochează SOS-ul dacă lipsesc.
- `POWER_UNSTABLE` și `LOW_VOLTAGE` sunt condiții tehnice; backendul nu estimează procentul bateriei.
- La restart, providerul de recovery poate restitui sesiuni neterminate. Backendul marchează
  întreruperea, nu presupune că audio continuă, nu creează o sesiune nouă și republică starea.
- Ruta autorizată `POST /api/v1/sessions/{id}/location` păstrează implicit numai ultima locație
  validă în memorie. La alertare, cererea de notificare include această locație și vechimea ei,
  dacă există; lipsa ei nu blochează SOS-ul și coordonatele nu sunt inventate.

## Autorizare și idempotency

Comenzile sesiunii folosesc `Authorization: Bearer <control_token>`. Tokenul este scoped pe
o singură sesiune; `session_id` singur nu autorizează citirea sau modificarea datelor. Statusul
global folosește `X-Dashboard-Key` atunci când cheia este configurată. Secretele nu se trimit
în URL și cheia permanentă ElevenLabs nu părăsește backendul.

Fiecare comandă mutabilă are `event_id`. Retrimiterea aceleiași comenzi este idempotentă;
refolosirea ID-ului pentru alt payload produce `409 EVENT_ID_REUSED`.

Contractul complet al rutelor este în [`../shared/API_CONTRACT.md`](../shared/API_CONTRACT.md).

## ElevenLabs și fallback

Backendul emite un credential temporar, iar telefonul transmite audio direct către ElevenLabs.
Registry-ul vocal nu permite două conversații active pentru aceeași sesiune și urmărește
`connected`, `disconnected`, eroare API, eroare microfon, timeout și reconectare. Retry-urile
sunt limitate și progresive. O eroare vocală produce stare `DEGRADED`, fără a anula sesiunea;
SOS-ul, anularea autorizată și confirmarea alternativă din interfața web rămân disponibile.
Audio nu este stocat implicit.

Detaliile și checklistul manual sunt în [`docs/ELEVENLABS.md`](docs/ELEVENLABS.md).

## Testare

```bash
cd backend
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m compileall -q app tests
```

Testele automate folosesc adaptorul GPIO mock. Verificarea pinului fizic, a exactității pragului
de 5 secunde și a comportamentului electric necesită Raspberry Pi și joystick reale. Testele
live ElevenLabs se rulează separat, numai cu credențiale și dispozitive autorizate:

```bash
RUN_ELEVENLABS_LIVE=1 python -m pytest -m live -s tests/live
```

## Limite intenționate

- Integrarea Tiger/TimescaleDB este opțională și scrie tranzițiile asincron, fără să blocheze
  Safety Engine. Politica Tiger implicită marchează sesiunile stocate neterminate `INTERRUPTED`
  la restart și păstrează alertele active în istoric. Deploymentul poate înlocui această politică
  cu un `SessionRecoveryProvider` persistent dacă trebuie să reconstruiască o sesiune live prin
  `RecoveredSessionBundle`; cele două politici nu se rulează pentru aceeași sesiune stocată.
- `InMemoryAuthorizedLocationStore` este adaptorul implicit pentru ultima locație autorizată.
  Persistența locației între restarturi necesită un `AuthorizedLocationStore` implementat și
  injectat de componenta de integrare; Agent 2 nu modifică schema de date.
- Frontendul, aplicația Android, notificarea efectivă a contactului și deploymentul nu sunt
  implementate aici. Backendul doar publică cererea de notificare.
- Backendul nu pornește microfonul Android, nu selectează Bluetooth și nu proxiază sau stochează
  audio.
- Nu există apeluri reale către 112 sau alte servicii de urgență.
