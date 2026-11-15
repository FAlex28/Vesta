# GPS Vesta pe Raspberry Pi → backend pe laptop

> **Opțional.** Acum locația vine de la GPS-ul telefonului (trimisă automat la 5 s). Serviciul de
> mai jos rămâne pentru varianta cu modul GPS pe Pi.

Serviciul `vesta-gps` citește modulul GPS (de ex. NEO-6M) pe portul serial al Pi-ului și trimite
poziția la `POST /api/v1/device/location` la fiecare 2 secunde, cu aceeași cheie și adresă ca
joystickul (`/etc/vesta/joystick.env`). Backendul o atașează sesiunii care rulează. Dispeceratul
și persoana de încredere o văd doar în timpul unei alerte.

## Cablaj (UART = GPIO14/15)

| Modul GPS | Raspberry Pi | Pin fizic |
| --- | --- | --- |
| **TX** | GPIO15 (RXD) | **10** |
| **RX** | GPIO14 (TXD) | **8** |
| VCC | 3,3 V sau 5 V (după modul; NEO-6M pe placă GY-GPS6MV2 merge cu 5 V) | **1** (3,3 V) / **2** (5 V) |
| GND | GND | **9** (pinul 6 e deja la joystick) |

TX-ul GPS-ului merge la RX-ul Pi-ului și invers. Cu firele inversate, Pi-ul nu primește nimic.

## Instalare (o dată, de pe laptop, după joystick)

```powershell
powershell -ExecutionPolicy Bypass -File .\pi\gps_forwarder\deploy.ps1
```

Dacă scriptul pornește UART-ul sau oprește consola serială, Pi-ul trebuie repornit o dată.

## Verificare

| Ce | Comandă (pe Pi) |
| --- | --- |
| Jurnal | `journalctl -u vesta-gps -f` |
| Ce trimite modulul (fără backend) | `sudo systemctl stop vesta-gps; python3 ~/vesta-gps/gps_forwarder.py --watch` |

Mesajele din jurnal:

- `Nicio dată de la modulul GPS` — fire, alimentare sau TX/RX inversate;
- `GPS-ul răspunde, dar nu are încă fix` — totul e conectat; afară sau lângă fereastră, primul
  fix durează 1–5 minute (LED-ul modulului începe să clipească);
- `Poziție trimisă … sesiune activă` — merge.

Precizia afișată este estimată din HDOP (HDOP × 5 m), nu măsurată.

Teste: `python -m pytest pi/gps_forwarder -q`.
