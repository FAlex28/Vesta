# Joystick Vesta pe Raspberry Pi → backend pe laptop

Pentru varianta în care backendul Vesta rulează pe laptop, iar joystickul e pe Raspberry Pi.
Serviciul `vesta-joystick` de pe Pi citește joystickul pe **GPIO17 (pinul fizic 11, GND pe
pinul 6)**. La o ținere de 5 secunde trimite o singură activare la
`POST /api/v1/device/events` (contractul API), cu antetul `X-Device-Key`.

```
joystick --GPIO17--> Pi: vesta-joystick --HTTP (rețeaua locală)--> laptop: backend :8000
```

Când backendul rulează direct pe Pi (`deploy/raspberry-pi`), acest serviciu nu e necesar:
backendul citește singur GPIO17. Nu le porni pe amândouă pe același Pi.

## Instalare (o singură dată, de pe laptop)

```powershell
powershell -ExecutionPolicy Bypass -File .\pi\joystick_forwarder\deploy.ps1
```

Scriptul:

- creează cheia dispozitivului în `backend\.state\device.key` (nu ajunge în git);
- copiază programul în `~/vesta-joystick`;
- scrie `/etc/vesta/joystick.env` (chmod 600; adresa laptopului `http://<nume-laptop>.local:8000`
  și cheia);
- pornește serviciul, care repornește singur și la pornirea Pi-ului.

Implicit se conectează la `alex@172.20.10.9`; altă adresă se dă cu `-Target alex@<ip>`.

## Pe laptop

Backendul trebuie să accepte conexiuni din rețea și să folosească aceeași cheie:

```powershell
cd backend
powershell -ExecutionPolicy Bypass -File .\start-lan.ps1
```

La prima rulare Windows cere o confirmare pentru regula de firewall, care permite portul 8000
doar din rețeaua locală.

## Verificare și depanare

| Ce | Comandă (pe Pi) |
| --- | --- |
| Jurnal live (apăsări, răspunsul backendului) | `journalctl -u vesta-joystick -f` |
| Doar cablajul, fără trimitere | `sudo systemctl stop vesta-joystick; python3 ~/vesta-joystick/joystick_forwarder.py --watch` |
| Backendul răspunde? | `curl http://Cristi_Desktop.local:8000/health` |
| Repornire serviciu | `sudo systemctl restart vesta-joystick` |

Mesajele din jurnal:

- `ACTIVATED` — sesiune nouă;
- `ALREADY_ACTIVE` — exista deja o sesiune;
- `HTTP 401` — cheia diferă; rulează din nou `deploy.ps1` și repornește `start-lan.ps1`;
- `Backend indisponibil` — laptopul e oprit, e în altă rețea sau firewall-ul blochează portul.

O apăsare care nu ajunge la backend în 2 minute e abandonată, nu pornește o sesiune
întârziată. Reîncercările păstrează același `event_id`, deci nu se creează sesiuni duble.

Teste: `python -m pytest pi/joystick_forwarder -q`.
