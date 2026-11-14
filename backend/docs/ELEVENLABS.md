# Integrarea vocală ElevenLabs

## Arhitectură

Vesta folosește un flux cu latență redusă, în care backendul păstrează cheia permanentă,
iar telefonul transportă audio direct către ElevenLabs:

```text
Android -> POST /sessions/{id}/voice/access -> FastAPI
FastAPI -> GET /v1/convai/conversation/token -> ElevenLabs
Android <- token WebRTC temporar <- FastAPI
Android <================ audio WebRTC ================> ElevenLabs
```

Raspberry Pi/FastAPI nu proxiază audio. Astfel se elimină un hop de rețea și cheia
`ELEVENLABS_API_KEY` rămâne exclusiv în headerul server-side `xi-api-key`.

Surse oficiale folosite pentru integrarea curentă:

- [Get conversation token (WebRTC)](https://elevenlabs.io/docs/eleven-agents/api-reference/conversations/get-webrtc-token)
- [ElevenLabs Agents SDK pentru Kotlin/Android](https://elevenlabs.io/docs/eleven-agents/libraries/kotlin)
- [Autentificarea agenților privați](https://elevenlabs.io/docs/eleven-agents/customization/authentication)
- [Configurarea limbii și vocii](https://elevenlabs.io/docs/eleven-agents/customization/voice/customization/language)
- [Optimizarea latenței](https://elevenlabs.io/docs/eleven-api/guides/how-to/best-practices/latency-optimization)
- [Erori ElevenLabs](https://elevenlabs.io/docs/eleven-api/resources/errors)

## Configurarea agentului

1. Creează în ElevenLabs un agent privat destinat mediului de test.
2. Configurează româna ca limbă principală sau limbă suplimentară și alege o voce
   verificată manual pentru limba/accentul țintă.
3. Păstrează `ELEVENLABS_API_KEY` și `ELEVENLABS_AGENT_ID` numai în mediul procesului
   FastAPI.
4. Configurează agentul sau clientul să folosească promptul, primul mesaj și metadata
   `client_tools` returnate de `/voice/access`. Acest backend nu modifică automat agentul
   din contul ElevenLabs.
5. Activează doar override-urile necesare în configurația agentului, dacă SDK-ul Android
   aplică `client_config` la pornirea conversației.

Promptul Vesta impune conversație calmă și naturală în română și interzice agentului să
declare o agresiune, să promită contactarea serviciilor de urgență sau să schimbe starea
de siguranță. Cele trei client tools sunt:

- `vesta_confirm_safe` -> clientul apelează endpointul Vesta `/checkins` numai după o
  confirmare explicită și neambiguă;
- `vesta_request_help` -> clientul apelează `/sos` la cererea explicită de ajutor;
- `vesta_report_technical_issue` -> clientul apelează `/technical-status` pentru probleme
  de microfon, audio sau rețea.

Tool-call-ul AI este un semnal de intrare. Doar motorul Python validează comanda și aplică
tranziția. Agentul nu are o cale directă spre `ALERT` în afara endpointului SOS autorizat.

## Flux Android

1. Obține `session.id` și `control_token` din activarea Vesta.
2. Cere credentialul de voce chiar înainte de conectare:

```http
POST /api/v1/sessions/{session_id}/voice/access
Authorization: Bearer <control_token>
Content-Type: application/json

{
  "transport": "webrtc",
  "participant_name": "demo-user",
  "environment": "unihack-demo"
}
```

3. În modul live, pornește SDK-ul oficial cu `conversation_token`; nu trimite cheia API
   permanentă telefonului. Tokenul trebuie tratat ca efemer și nu trebuie salvat.
4. Conectează `client_tools` la endpointurile Vesta, reutilizând tokenul de control în
   headerul `Authorization` și generând un `event_id` unic pentru fiecare acțiune.
5. Trimite periodic `POST /api/v1/sessions/{id}/heartbeat` cu `{}` cât timp clientul audio
   este conectat, recomandat la mai puțin de jumătate din
   `VESTA_CLIENT_HEARTBEAT_TIMEOUT_SECONDS`. Expirarea lease-ului devine
   `network_disconnected`, nu alertă de siguranță.
6. Ascultă `WS /ws/events`. La `safety.check_in_requested`, trimite agentului un contextual
   update `safety_check_requested`, astfel încât să ceară verbal confirmarea.
7. La oprire sau reconectare, încheie sesiunea SDK existentă și creează una nouă conform
   comportamentului SDK-ului curent.

Permisiuni Android necesare pentru integrarea clientului:

```xml
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.RECORD_AUDIO" />
<uses-permission android:name="android.permission.MODIFY_AUDIO_SETTINGS" />
```

`RECORD_AUDIO` trebuie cerută explicit la runtime. Pornirea microfonului în fundal,
selectarea căștilor Bluetooth și rutarea audio sunt responsabilitatea aplicației Android;
backendul doar emite credentialul și procesează statusurile raportate.

## Mod demo

Cu `VESTA_VOICE_MODE=demo`, endpointul `/voice/access` nu face cereri externe și
returnează un răspuns marcat explicit:

```json
{
  "provider": "demo",
  "mode": "demo",
  "transport": "scripted",
  "conversation_token": null,
  "conversation_id": "demo-<session_id>",
  "client_config": {
    "language": "ro",
    "demo_messages": ["..."]
  }
}
```

Datele demo nu trebuie prezentate ca o conversație live ElevenLabs.

## Erori și fallback

Providerul live diferențiază:

| Situație ElevenLabs | Cod Vesta | Retry provider |
| --- | --- | --- |
| cheie/agent lipsă sau agent inexistent | `VOICE_NOT_CONFIGURED` | nu |
| `401` / `403` | `VOICE_AUTHENTICATION_FAILED` | nu |
| `402` | `VOICE_QUOTA_UNAVAILABLE` | nu |
| `429` | `VOICE_RATE_LIMITED` | o singură dată, apoi eroare |
| timeout / răspuns invalid / `5xx` | `VOICE_PROVIDER_UNAVAILABLE` | o dată doar pentru `503`; celelalte eșuează controlat |

La eșec, API-ul răspunde `503`, publică `technical.issue_reported` pentru
`voice_unavailable` și suspendă escaladarea automată bazată pe lipsa răspunsului. SOS-ul
explicit rămâne disponibil. O cerere de acces vocal reușită rezolvă problema tehnică și
publică `technical.issue_resolved` dacă aceasta fusese activă.

Clientul trebuie să raporteze separat:

- microfon refuzat -> `microphone_denied`;
- pierderea rețelei -> `network_disconnected`;
- întreruperea căștilor/audio -> `audio_interrupted`;
- revenirea componentei -> același `issue`, cu `active: false`.

Nicio eroare audio sau de rețea nu este interpretată automat drept agresiune.

## Checklist manual Android

Rularea acestui checklist necesită telefonul și căștile de test autorizate. Notează pentru
fiecare rulare modelul telefonului, versiunea Android, căștile, tipul rețelei, agentul/vocea
și ora testului.

### Conectare și latență

- [ ] Permisiunea de microfon este cerută vizibil înainte de sesiune.
- [ ] Tokenul WebRTC este cerut imediat înainte de conectare și nu este persistat.
- [ ] Măsoară separat: activare -> răspuns `/voice/access`, token -> callback `onConnect`
  și finalul replicii utilizatoarei -> începutul răspunsului agentului.
- [ ] Rulează minimum cinci conversații pe aceeași rețea și notează mediană, maxim și
  orice reconectare.
- [ ] Confirmă că audio circulă direct telefon-ElevenLabs, nu prin FastAPI/Raspberry Pi.

### Calitatea conversației în română

- [ ] Primul mesaj este natural, inteligibil și în română.
- [ ] Agentul păstrează limba română după replici scurte, zgomot și pauze.
- [ ] Numele/locurile românești și întrebarea „Ești în regulă?” sunt pronunțate clar.
- [ ] Agentul nu afirmă că a detectat o agresiune și nu pretinde că a apelat 112.
- [ ] O confirmare ambiguă produce clarificare, nu `vesta_confirm_safe` imediat.

### Întrerupere și check-in

- [ ] Utilizatoarea poate întrerupe agentul fără blocarea conversației.
- [ ] `safety.check_in_requested` produce contextual update și o întrebare verbală clară.
- [ ] O confirmare explicită apelează o singură dată `/checkins` cu `event_id` unic.
- [ ] Lipsa răspunsului produce retry-ul Python, apoi alerta deterministă conform timpilor.
- [ ] O cerere explicită de ajutor apelează `/sos` chiar dacă serviciul vocal este degradat.

### Microfon, Bluetooth și rețea

- [ ] Refuză `RECORD_AUDIO`: clientul raportează `microphone_denied`, iar backendul nu
  trece automat în `ALERT`.
- [ ] Deconectează și reconectează căștile: clientul raportează/rezolvă
  `audio_interrupted` și selectează corect ruta audio.
- [ ] Oprește temporar rețeaua: clientul raportează `network_disconnected` la revenire și
  recreează conversația fără reutilizarea tokenului WebRTC.
- [ ] Simulează provider indisponibil/rate limit: UI-ul afișează fallbackul, iar SOS-ul
  rămâne funcțional.
- [ ] Inspectează logurile, traficul clientului și răspunsurile: cheia permanentă
  `ELEVENLABS_API_KEY` nu apare nicăieri în afara cererii server-ElevenLabs.

