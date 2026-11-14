# Vesta · Frontend

Aplicație web React + TypeScript + Vite cu trei ecrane:

| Rută | Pentru cine | Ce face |
| --- | --- | --- |
| `/` | operatorul / prezentarea | Dashboard: starea sesiunii, conexiunea telefonului, vocea, GPS, alimentarea Raspberry Pi, alertele active și rezolvate, jurnalul live și codurile de asociere pentru telefon și contact. Nu afișează coordonate reale; harta arată doar punctul fix de test. |
| `/companion` | utilizatoarea, pe telefonul Android | Asocierea cu un cod, preluarea automată a sesiunii pornite din joystick, „Sunt bine”, SOS, anulare/încheiere, închiderea alertei, conversația vocală ElevenLabs, GPS opțional și starea conexiunii. |
| `/contact` | contactul de încredere | Acces cu un cod; alerta activă cu ultima locație autorizată (doar cât timp alerta e activă), alertele rezolvate cu ora închiderii. |

Datele și comenzile urmează `shared/API_CONTRACT.md` v3.

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

1. Dashboardul (autentificat cu `VESTA_DASHBOARD_API_KEY`, ținută doar în memoria filei) creează un cod de 8 caractere, valabil 10 minute și o singură dată.
2. Telefonul introduce codul în `/companion` și primește o asociere limitată la acel dispozitiv (24 h, păstrată pe telefon, „Uită asocierea” o șterge). Telefonul întreabă serverul la 3 s dacă rulează o sesiune; când joystickul pornește una, o preia singur. Tokenul de control al sesiunii nu este salvat niciodată: e cerut din nou de la server.
3. Contactul introduce un cod de contact în `/contact` și vede doar alertele acelui dispozitiv. Deschiderea unei alerte active este raportată serverului ca „văzută” (`viewed_at`).

## Ce garantează interfața

- Starea sesiunii vine numai de la server. Frontendul nu cronometrează apăsarea de 5 secunde și nu afișează ACTIVE înainte de confirmarea serverului.
- Evenimentele sunt deduplicate după `event_id` și `sequence`. După reconectare sau repornirea serverului, starea se reîncarcă de la server.
- SOS, „Sunt bine”, anularea și închiderea alertei afișează succes doar după răspunsul serverului; un retry reutilizează același `event_id`.
- O verificare afișată pe pagina deschisă a telefonului este raportată ca livrată (`channel: android`). Dacă două verificări livrate rămân fără răspuns, serverul trece în ALERT.
- Telefonul raportează vizibilitatea paginii, disponibilitatea GPS și starea vocii; coordonatele pleacă doar prin comutatorul explicit de partajare, oprit implicit.
- Nu există procent de baterie: alimentarea Pi-ului vine din `vcgencmd`.

Limite: comportamentul cu ecranul blocat depinde de browserul Android și trebuie verificat pe telefonul real. Nu există evenimente de progres pentru apăsarea de 5 s. Vesta nu apelează 112 și nu trimite SMS sau push; contactul vede alerta doar pe pagina `/contact` deschisă.

## Teste

```bash
npm run build
npm test
```

`npm test` rulează Playwright (Chrome instalat pe sistem) cu backend complet mock-uit în browser. Mockul respinge, ca backendul real, câmpurile și valorile necunoscute (422). Proxy-ul spre backend este îndreptat spre un port închis, deci nicio cerere nu ajunge la un server real. Microfonul, GPS-ul și SDK-ul ElevenLabs sunt înlocuite; nu se cer permisiuni reale și nu se declanșează SOS real.
