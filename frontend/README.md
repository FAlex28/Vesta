# Vesta · Frontend

Aplicație web React + TypeScript + Vite cu trei ecrane:

| Rută | Pentru cine | Ce face |
| --- | --- | --- |
| `/` | dispeceratul | Starea sesiunii, conexiunea telefonului, vocea, GPS, alimentarea Raspberry Pi, alertele active și rezolvate, jurnalul live; locația reală doar în timpul unei alerte active (ascunsă în modul prezentare). |
| `/companion` | persoana protejată, pe telefonul Android | Asocierea cu QR-ul de pe dispozitiv, preluarea automată a sesiunii pornite din joystick, „Sunt bine”, SOS, anulare/încheiere, închiderea alertei, conversația vocală ElevenLabs, GPS opțional, contactele de urgență și starea conexiunii. |
| `/contact` | contactul de urgență | Acces prin linkul din SMS; notificări push; alerta activă cu ultima locație autorizată (doar cât timp alerta e activă), alertele rezolvate cu ora închiderii. |

Datele și comenzile urmează `shared/API_CONTRACT.md` v4. Interfața folosește tema întunecată „Vesta Night”: culoarea poartă doar starea.

## Rulare

Pe Raspberry Pi, backendul servește direct `frontend/dist` (`VESTA_FRONTEND_DIST`), deci totul are o singură adresă: `http://<pi>:8000/`. Pașii sunt în `deploy/raspberry-pi/README.md`.

Pentru dezvoltare pe laptop:

```bash
npm install
npm run dev
```

Serverul Vite redirecționează `/api`, `/ws` și `/health` către backend (`VESTA_BACKEND_URL`, implicit `http://127.0.0.1:8000`). Adresa Vite folosită de telefon trebuie adăugată în `VESTA_CORS_ORIGINS` pe backend.

**Microfonul și GPS-ul cer HTTPS** (sau `localhost`). Pe telefon folosește tunelul HTTPS din `deploy/raspberry-pi` sau, pentru o rețea locală de test, opțiunea Chrome descrisă acolo.

| Variabilă | Rol |
| --- | --- |
| `VESTA_BACKEND_URL` | Ținta proxy-ului Vite (doar la dev/preview). |
| `VITE_VESTA_API_URL` | Origine backend directă, dacă nu se folosește proxy-ul. |
| `VITE_VESTA_DEVICE_ID` | Dispozitivul urmărit când serverul are mai multe sesiuni. |
| `VITE_VESTA_MANUAL_PAIRING` | `true` afișează și formularul de test cu identificatorul sesiunii și tokenul de control. |

## Asocierea și accesul

1. **Telefonul persoanei** scanează QR-ul de pe dispozitivul Vesta (`/companion#pair=COD`) sau scrie codul de 10 caractere. Primește o asociere limitată la acel dispozitiv (30 de zile, păstrată pe telefon, revocabilă din lista „Telefoane asociate”). Telefonul întreabă serverul la 3 s dacă rulează o sesiune; când joystickul pornește una, o preia singur. Tokenul de control al sesiunii nu este salvat: e cerut din nou de la server.
2. **Contactele de urgență** le alege persoana, pe telefonul ei: nume și număr → invitație de unică folosință → **Trimite prin SMS** (se deschide aplicația de mesaje cu textul gata). Contactul deschide `/contact#cod=COD`, vede doar alertele acelei persoane și apasă **Permite notificările**; alertele ajung apoi ca notificări push chiar cu pagina închisă (`public/sw.js`).
3. **Dispeceratul** (`/`, cheia `VESTA_DASHBOARD_API_KEY` ținută doar în memoria filei) doar monitorizează. În timpul unei alerte active vede ultima locație autorizată; **Mod prezentare** ascunde coordonatele.

Codurile din link sunt citite din fragmentul URL (nu ajung la server sau în loguri) și șterse imediat din bara de adrese.

## Ce garantează interfața

- Starea sesiunii vine numai de la server. Frontendul nu cronometrează apăsarea de 5 secunde și nu afișează ACTIVE înainte de confirmarea serverului.
- Evenimentele sunt deduplicate după `event_id` și `sequence`. După reconectare sau repornirea serverului, starea se reîncarcă de la server.
- SOS, „Sunt bine”, anularea și închiderea alertei afișează succes doar după răspunsul serverului; un retry reutilizează același `event_id`.
- O verificare afișată pe pagina deschisă a telefonului este raportată ca livrată (`channel: android`). Dacă două verificări livrate rămân fără răspuns, serverul trece în ALERT.
- Telefonul raportează vizibilitatea paginii, disponibilitatea GPS și starea vocii; coordonatele pleacă doar prin comutatorul explicit de partajare, oprit implicit.
- Nu există procent de baterie: alimentarea Pi-ului vine din `vcgencmd`.

Limite: comportamentul cu ecranul blocat și livrarea notificărilor push depind de Android/Chrome și trebuie verificate pe telefoanele reale. Nu există evenimente de progres pentru apăsarea de 5 s. Vesta nu apelează 112 și nu trimite SMS de pe server.

## Teste

```bash
npm run build
npm test
```

`npm test` rulează Playwright (Chrome instalat pe sistem) cu backend complet mock-uit în browser. Mockul respinge, ca backendul real, câmpurile și valorile necunoscute (422). Proxy-ul spre backend este îndreptat spre un port închis, deci nicio cerere nu ajunge la un server real. Microfonul, GPS-ul și SDK-ul ElevenLabs sunt înlocuite; nu se cer permisiuni reale și nu se declanșează SOS real.
