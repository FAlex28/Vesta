# Bridge PC ↔ Arduino Nano

`nano_bridge.py` rulează pe calculatorul (sau Raspberry Pi-ul) la care e legat Nano-ul prin USB.
Urmărește backendul Vesta și trimite plăcii comenzile din
[`../vesta_dispatcher/README.md`](../vesta_dispatcher/README.md).

```
backend  --WebSocket /ws/events-->  bridge  --GET /api/v1/status-->  stare
                                      |
                                      +--USB serial 115200-->  Nano (LED-uri, buzzere, LCD)
```

Evenimentele WebSocket doar declanșează o recitire a `/api/v1/status`; starea afișată vine
mereu din status (plus o recitire la 5 s). Bridge-ul trimite `PING` la 3 s, chiar și când
backendul nu răspunde, așa că placa nu ajunge la eroarea `CONEXIUNE PC` cât timp bridge-ul
rulează. La oprire (Ctrl+C) trimite `BYE`.

## Instalare

```powershell
cd hardware\arduino_nano\bridge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Încarcă întâi `vesta_dispatcher.ino` pe placă din Arduino IDE (biblioteca
*LiquidCrystal I2C* de Frank de Brabander, placa *Arduino Nano*). Placa Vesta (clonă CH340)
cere **Processor: ATmega328P (Old Bootloader)**; cu varianta nouă uploadul dă
`not in sync: resp=0x00`. **Închide Serial Monitor** înainte să pornești bridge-ul:
portul poate fi deschis de un singur program.

## Pornire rapidă pe Windows

```powershell
cd hardware\arduino_nano\bridge
powershell -ExecutionPolicy Bypass -File .\start-bridge.ps1 -Port COM7
```

La prima rulare creează `.venv` și instalează dependențele. Pornește `bridge` cu
`--keep-running`: după o eroare neprevăzută se repornește singur în 3 s, deci placa primește
`PING` în continuu. Ctrl+C trimite `BYE`, iar placa rămâne liniștită în „Astept PC”.

## Testare, pas cu pas

| Ce testezi | Comandă |
| --- | --- |
| Ce porturi există | `python nano_bridge.py ports` |
| Placa singură (LED-uri, buzzere, LCD), fără backend | `python nano_bridge.py demo --port COM5` |
| La fel + pierderea legăturii (10 s fără PING) | `python nano_bridge.py demo --port COM5 --with-timeout` |
| Comenzi de mână | `python nano_bridge.py manual --port COM5` |
| Totul fără placă (simulator text) | adaugă `--port sim` la orice mod |
| Logica, fără placă și fără backend | `python -m pytest test_nano_bridge.py -q` |

`--port` poate lipsi dacă e o singură placă Arduino/CH340/FTDI conectată.
Demo-ul spune la fiecare pas ce trebuie să vezi și să auzi.

## Cu backendul (fără Pi și fără telefon)

1. Backendul pe laptop, în modul simulat (vezi `backend/README.md`), de exemplu cu
   `VESTA_CHECKIN_INTERVAL_SECONDS=10` ca verificările să vină repede.
2. Bridge-ul: `python nano_bridge.py bridge --port COM5`
3. Într-un alt terminal, „joystickul și telefonul” simulate:

   ```powershell
   python nano_bridge.py simulate
   ```

   `a` = activare (verde), după interval vine verificarea (galben), `d` + `ok` = „Sunt bine”,
   `s` = SOS (roșu + alarmă), `r` = închide alerta, `t microphone_denied` / `... off` =
   eroare tehnică (albastru), `c` = încheie sesiunea. Merge și neinteractiv:
   `python nano_bridge.py simulate --script "a;w 12;d;ok;s;w 3;r"`.

## Pe Raspberry Pi (producție)

```bash
python3 nano_bridge.py bridge --port /dev/ttyUSB0 --dashboard-key "$VESTA_DASHBOARD_API_KEY"
```

Cheia se poate da și prin variabila `VESTA_DASHBOARD_API_KEY`; backendul prin `--backend`
sau `VESTA_BACKEND_URL` (implicit `http://127.0.0.1:8000`). Doar sesiunile dispozitivului
`--device-id` (implicit `vesta-rpi-01`) sunt afișate; `--all-devices` scoate filtrul.

## Ce afișează placa

| Backend | Comandă trimisă |
| --- | --- |
| fără sesiune / `CANCELLED` | `STATE\|STANDBY` (alb) |
| `ACTIVE` | `STATE\|ACTIVE` (verde) |
| `CHECK_IN`, `UNCONFIRMED` | `STATE\|CHECK` (galben) |
| alertă `ACTIVE` | `ALERT_RAISED\|<alert_id>\|<motiv>` (roșu + D7) |
| alerta devine `RESOLVED` | `ALERT_RESOLVED\|<alert_id>` |
| problemă tehnică a sesiunii (rețea, microfon, audio, voce, alimentare, repornire) sau alimentarea Pi-ului | `TECHNICAL_ERROR\|<cod>` (albastru + D8) |
| backend inaccesibil mai mult de 15 s | `TECHNICAL_ERROR\|SERVER OFFLINE` |

GPS-ul și pagina ascunsă nu aprind LED-ul albastru: nu sunt defecte ale lanțului de siguranță.

Reguli de siguranță:

- **Alarma se oprește numai la închiderea explicită.** Bridge-ul trimite `ALERT_RESOLVED`
  doar când backendul raportează alerta `RESOLVED`. Dacă alerta dispare fără să fi fost
  închisă (de exemplu backendul a repornit fără persistență), alarma continuă și bridge-ul
  afișează în consolă comanda de oprire manuală.
- **Backend căzut:** placa păstrează ultima stare, iar alerta continuă să sune. Un restart
  scurt al backendului (sub 15 s) nu aprinde eroarea.
- **Resincronizare:** la `BOOT` (placa s-a resetat) sau la reconectarea USB, starea completă
  se retrimite.
