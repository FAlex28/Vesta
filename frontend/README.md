# Vesta · Frontend

Aplicație web React + TypeScript + Vite cu trei ecrane:

| Rută | Pentru cine | Ce face |
| --- | --- | --- |
| `/` | echipa / prezentare | Dashboard doar pentru citire: starea sesiunii, conexiunea telefonului, vocea, GPS, alimentarea, alertele active și rezolvate, jurnalul live. Harta arată doar punctul fix de test. |
| `/companion` | utilizatoarea, pe telefonul Android | Starea sesiunii, „Sunt bine”, SOS, anularea sau încheierea sesiunii, conversația vocală ElevenLabs, GPS opțional și starea conexiunii. |
| `/contact` | contactul de încredere (test) | Alerta activă sau rezolvată, cu ora închiderii. Fără coordonate. |

Datele și comenzile urmează `shared/API_CONTRACT.md` v2: starea globală este citită din `GET /api/v1/status`, telefonul folosește rute de sesiune cu token, iar fluxul WebSocket este limitat la sesiunea autorizată.

## Rulare locală

```bash
npm install
npm run dev
```

Serverul Vite redirecționează `/api`, `/ws` și `/health` către backend (`VESTA_BACKEND_URL`, implicit `http://127.0.0.1:8000`). Telefonul are nevoie de o singură adresă. Adresa folosită de telefon (de exemplu `http://192.168.1.20:5173`) trebuie adăugată în `VESTA_CORS_ORIGINS` pe backend, altfel WebSocketul este refuzat.

**Microfonul și GPS-ul funcționează doar pe HTTPS** (sau `localhost`). Pe telefon, pagina trebuie servită prin HTTPS; altfel `/companion` afișează explicit că microfonul și locația sunt blocate de browser.

| Variabilă | Rol |
| --- | --- |
| `VESTA_BACKEND_URL` | Ținta proxy-ului Vite (doar la dev/preview). |
| `VITE_VESTA_API_URL` | Origine backend directă, dacă nu se folosește proxy-ul. |
| `VITE_VESTA_DEVICE_ID` | Dispozitivul urmărit când serverul are mai multe sesiuni. |
| `VITE_VESTA_MANUAL_PAIRING` | `true` afișează în `/companion` formularul de asociere manuală, **doar pentru teste controlate** (vezi mai jos). |

Într-un mediu securizat, dashboardul cere cheia `VESTA_DASHBOARD_API_KEY` emisă de backend și o ține doar în memoria filei. Telefonul poate fi asociat manual cu identificatorul sesiunii și `control_token`; tokenul este verificat prin ruta v2 protejată și WebSocketul primește doar evenimentele acelei sesiuni. Nu pune cheile în variabile `VITE_*` sau în bundle-ul public.

## Ce este real și ce lipsește

- Starea sesiunii (IDLE, ACTIVE, CHECK_IN, UNCONFIRMED, ALERT, CANCELLED) vine numai de la server. Frontendul nu cronometrează apăsarea de 5 secunde și nu afișează ACTIVE înainte de confirmarea serverului.
- Evenimentele sunt deduplicate după `event_id` și `sequence`. După reconectare sau repornirea serverului, starea se reîncarcă din `GET /api/v1/status`; nimic nu se restaurează din memoria browserului.
- Tokenul de control al sesiunii stă doar în memoria paginii: nu ajunge în URL, `localStorage` sau loguri.
- SOS, „Sunt bine” și anularea afișează succes doar după răspunsul serverului. Un retry reutilizează același `event_id`.

Integrarea aplicației cu firmware-ul și serviciile externe mai are aceste limite:

1. **Asocierea telefonului cu sesiunea.** Tokenul este păstrat numai în memoria paginii și poate fi introdus manual doar cu `VITE_VESTA_MANUAL_PAIRING=true`; în medii securizate se introduc identificatorul sesiunii și tokenul. Firmware-ul nu are încă un transfer agreat către telefon.
2. Evenimentele de progres pentru apăsarea joystickului timp de 5 secunde.
3. GPS-ul este solicitat numai după acțiunea utilizatoarei. Trimiterea către server este o alegere separată, oprită implicit, și funcționează doar cu o sesiune asociată și activă. Dashboardul nu poate citi încă locația transmisă; harta lui rămâne un punct fix de referință.
4. Autorizarea distinctă a paginii contactului de încredere și livrarea efectivă a notificărilor.
5. Verificările reale pe hardware, Android și cu credențialele ElevenLabs de producție.

Vesta nu apelează 112 și nu trimite SMS sau notificări din frontend.

## Teste

```bash
npm run build
npm test
```

`npm test` rulează Playwright (Chrome instalat pe sistem) cu backend complet mock-uit în browser. Proxy-ul spre backend este îndreptat spre un port închis, deci nicio cerere nu ajunge la un server real. Microfonul, GPS-ul și SDK-ul ElevenLabs sunt înlocuite în teste; nu se cer permisiuni reale și nu se declanșează SOS real.
