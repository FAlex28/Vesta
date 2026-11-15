# Raspberry Pi + Arduino Nano virtuale

Testează tot lanțul hardware fără plăci, cu **programele reale** de pe dispozitive. Se
înlocuiește doar hardware-ul de sub ele, iar backendul primește exact aceleași cereri ca live.

```
/buton?pi ──press/release──> Pi virtual: GPIO17 (gpiozero MockFactory)
                               └─ pi/joystick_forwarder (real) ──POST /api/v1/device/events──┐
                             GPS virtual: NMEA GGA+RMC                                        │
                               └─ pi/gps_forwarder (real) ──POST /api/v1/device/location─────┤
                                                                                        backend
Nano virtual (sketch-ul .ino oglindit) <──socket://127.0.0.1:7701── nano_bridge.py (real) <──┘
                                                                   (/api/v1/status + /ws/events)
```

| Componentă live | În simulare | Cod care rulează |
| --- | --- | --- |
| Joystick pe GPIO17 | pin `MockFactory` | `joystick_forwarder.run_forwarder`: debounce, ținere 5 s, reîncercări, cheie |
| Modul GPS pe UART | propoziții NMEA valide, 1 Hz, traseu în Timișoara | `gps_forwarder.run`: parser NMEA, trimitere la 2 s |
| Nano pe USB (COM / ttyUSB) | `virtual_nano.py` pe un socket TCP | `nano_bridge.py bridge --keep-running`, aceeași linie de comandă |

Placa virtuală reproduce sketch-ul: răspunsurile, trunchierile buffer-elor, `LINK|LOST` după
10 s fără PING, prioritatea LED-urilor, textele LCD, buzzerele și resetul la deschiderea portului.

## Pornire (3 terminale)

```powershell
cd backend;  powershell -ExecutionPolicy Bypass -File .\start-lan.ps1      # backend + cheia dispozitivului
cd frontend; npm run dev                                                    # sau .\start-lan.ps1
powershell -ExecutionPolicy Bypass -File .\hardware\simulator\start-sim.ps1  # Pi + Nano virtuale
```

Deschide **http://localhost:5173/buton?pi** și ține butonul apăsat 5 s. Pagina apasă GPIO17 pe
Pi-ul virtual, iar panoul de sub buton arată LED-ul și LCD-ul Nano-ului. Fără `?pi`, pagina
funcționează ca înainte (`/api/v1/simulator/press`). Dispeceratul (`/`) și telefonul
(`/companion`) se folosesc normal.

În consolă apar, cu prefix, joystickul și GPS-ul Pi-ului, bridge-ul și Nano-ul (LED / LCD / buzzer).

## Defecte simulate (API pe http://127.0.0.1:8790)

| Ce | Cerere |
| --- | --- |
| Starea completă | `GET /state` |
| Joystick ținut N secunde | `POST /gpio17/hold?seconds=5.5` |
| GPS fără fix / din nou cu fix | `POST /gps/fix?on=0` / `?on=1` |
| Modulul GPS nu mai trimite date | `POST /gps/module?connected=0` |
| Cablul USB al Nano-ului scos / pus | `POST /nano/usb?connected=0` / `?connected=1` |

## Teste

```powershell
python -m pytest hardware/simulator -q            # placa virtuală, GPS-ul, SerialLink pe socket
python hardware/simulator/scenario.py             # scenariul complet (21 de verificări)
```

`scenario.py` cere backendul în modul demo (`VESTA_OPEN_ACCESS=true`, setat deja de
`start-lan.ps1`) și verificări rapide, ca să nu dureze minute:

```powershell
$env:VESTA_FIRST_CHECKIN_DELAY_SECONDS=3; $env:VESTA_CHECKIN_RESPONSE_TIMEOUT_SECONDS=5
$env:VESTA_CHECKIN_RETRY_DELAY_SECONDS=2; $env:VESTA_CHECKIN_MAX_ATTEMPTS=2
```

Ce verifică:
- apăsarea scurtă e ignorată, iar ținerea de 5 s pornește o singură sesiune;
- Nano-ul trece prin verde, galben și roșu (alarmă D7), apoi „Alerta rezolvata”;
- un SOS de pe telefon apare pe placă;
- cu cablul USB scos în timpul alertei, alarma continuă, iar la reconectare se resincronizează;
- microfonul refuzat aprinde LED-ul albastru (D8);
- fără telefon nu apare o alertă falsă;
- ruta GPS a Pi-ului ajunge la dispecerat.

## Ce nu acoperă

- Cablajul și componentele fizice: pinii, rezistențele, LCD-ul I2C, sursa de alimentare.
  Pentru ele există `nano_bridge.py demo` și `joystick_forwarder.py --watch` pe plăcile reale.
- Camerele de pe Pi (`pi/camera_test`), care au nevoie de v4l2 / picamera.
- Citirea alimentării Pi-ului (`vcgencmd`), disponibilă doar când backendul rulează pe Pi.
