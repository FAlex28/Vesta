# Vesta pe Raspberry Pi: instalare pas cu pas

Rezultatul: Raspberry Pi 4 rulează tot (backend, regulile de siguranță, joystickul pe GPIO17 și aplicația web) pe portul 8000. Laptopul deschide dashboardul, telefonul deschide `/companion`, contactul deschide `/contact`.

## 1. Ce îți trebuie

- Raspberry Pi 4 și card microSD de minimum 16 GB.
- **Sursă oficială 5 V / 3 A USB-C.** O baterie externă slabă produce `LOW_VOLTAGE` și poate opri Pi-ul fără nicio alertă.
- Modul joystick (de tip KY-023, cu pin `SW`) și 2 fire dupont mamă–mamă.
- Telefon Android cu Chrome; căștile Razer împerecheate prin Bluetooth cu telefonul.
- Laptopul cu proiectul (Node.js 20.19+ pentru build).
- Internet pe Pi și pe telefon (de exemplu hotspotul telefonului). ElevenLabs și Tiger Data sunt servicii cloud.
- Opțional: cheie ElevenLabs + agent; URL Tiger Data.

## 2. Cablarea joystickului (cu Pi-ul oprit)

Se folosește doar butonul joystickului:

| Joystick | Raspberry Pi | Pin fizic |
| --- | --- | --- |
| `SW` | GPIO17 (BCM 17) | **11** |
| `GND` | GND | **6** |
| `+5V`, `VRx`, `VRy` | nu se conectează | — |

```
Header-ul de 40 de pini (vedere de sus). Pinul 1 are padul pătrat și e la capătul
header-ului cel mai departe de porturile USB-A / Ethernet; rândul cu pinii impari e spre interiorul plăcii.
 pin 1  3V3   ● ●  5V    pin 2
 pin 3  GPIO2 ● ●  5V    pin 4
 pin 5  GPIO3 ● ●  GND   pin 6   <- GND joystick
 pin 7  GPIO4 ● ●  GPIO14 pin 8
 pin 9  GND   ● ●  GPIO15 pin 10
 pin 11 GPIO17● ●  GPIO18 pin 12
        ^ SW joystick
```

Pull-up-ul intern e activat din software (`pull_up=True`), deci nu e nevoie de rezistor. **Nu lega `+5V` la un pin GPIO.**

## 3. Sistemul de operare

1. Pe laptop, deschide **Raspberry Pi Imager**: *Raspberry Pi 4* → *Raspberry Pi OS (64-bit)* (Bookworm, Python 3.11).
2. În setări (rotița): hostname `vesta`, un utilizator și o parolă, Wi-Fi-ul (sau hotspotul telefonului), **SSH activat**.
3. Pune cardul în Pi, alimentează-l, așteaptă 1–2 minute, apoi conectează-te: `ssh <utilizator>@vesta.local`. Dacă numele nu merge, găsește IP-ul Pi-ului în pagina routerului sau a hotspotului.

## 4. Copierea proiectului pe Pi

Pe laptop (PowerShell), din folderul proiectului:

```powershell
cd frontend; npm ci; npm run build; cd ..
tar -czf vesta.tgz --exclude=node_modules --exclude=.venv --exclude=.git --exclude=__pycache__ backend deploy shared frontend/dist
scp vesta.tgz <utilizator>@vesta.local:~
```

Pe Pi:

```bash
mkdir -p ~/Vesta && tar -xzf ~/vesta.tgz -C ~/Vesta
```

(Dacă proiectul e pe GitHub, poți face `git clone` pe Pi și copia doar `frontend/dist`.)

## 5. Instalarea

Pe Pi:

```bash
cd ~/Vesta
bash deploy/raspberry-pi/install.sh
```

Scriptul instalează Python, `gpiozero` și `lgpio`, creează `/etc/vesta/vesta.env` cu secrete generate aleator, instalează serviciul `vesta` (pornește singur la boot, repornește la eroare) și la final afișează **cheia dashboardului**. Păstreaz-o privat.

## 6. Configurarea

```bash
sudo nano /etc/vesta/vesta.env
```

- **Voce:** `VESTA_VOICE_MODE=elevenlabs`, `ELEVENLABS_API_KEY=...` și `ELEVENLABS_AGENT_ID=...`. Fără ele, pune `VESTA_VOICE_MODE=demo` (text în loc de voce).
  Agentul ElevenLabs trebuie creat o dată, conform `backend/docs/ELEVENLABS.md`:
  - limba română;
  - promptul din `backend/app/voice/prompt.py`;
  - client tools: `vesta_confirm_safe`, `vesta_request_help` și `vesta_report_technical_issue` (parametru text `issue`);
  - autentificare activată (agent privat).
- **Istoric Tiger Data (opțional):** `VESTA_PERSISTENCE=tiger` și `TIMESCALE_SERVICE_URL=postgres://...`, apoi aplică migrările o singură dată:

  ```bash
  cd ~/Vesta/backend
  set -a; source <(sudo cat /etc/vesta/vesta.env); set +a
  .venv/bin/python -m app.db.migrate
  ```

- **Ritmul verificărilor:** implicit, o verificare la 60 s, 15 s pentru răspuns și 2 încercări. Pentru o demonstrație scurtă poți pune `VESTA_CHECKIN_INTERVAL_SECONDS=30`.

Aplică modificările: `sudo systemctl restart vesta`.

## 7. Verificarea pe Pi

```bash
curl http://127.0.0.1:8000/health          # {"status":"ok",...}
journalctl -u vesta -f                      # logurile în timp real
sudo grep DASHBOARD /etc/vesta/vesta.env    # cheia dashboardului
```

Test joystick:

- o apăsare scurtă nu face nimic;
- ținut **5 secunde** continuu, apare o singură activare (în dashboard: ACTIVE);
- ținut 10 secunde, tot o singură activare;
- o nouă activare cere eliberare și apăsare din nou.

## 8. HTTPS pentru telefon (obligatoriu pentru microfon și GPS)

Chrome pe Android permite microfonul și locația doar pe HTTPS. Alege una dintre variante:

**A. Tunel HTTPS (recomandat; funcționează pe orice rețea cu internet)**

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o cloudflared
sudo install cloudflared /usr/local/bin/cloudflared
sed -e "s#__USER__#$(id -un)#g" ~/Vesta/deploy/raspberry-pi/vesta-tunnel.service | sudo tee /etc/systemd/system/vesta-tunnel.service
sudo systemctl daemon-reload && sudo systemctl enable --now vesta-tunnel
journalctl -u vesta-tunnel | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1
```

Adresa `https://....trycloudflare.com` se schimbă la fiecare repornire a tunelului. Toate datele rămân protejate de chei și coduri.

**B. Doar în rețeaua locală (fără internet pentru pagină)**

Pe telefon, în Chrome: `chrome://flags` → *Insecure origins treated as secure* → adaugă `http://<IP-ul Pi-ului>:8000` → *Enabled* → *Relaunch*.

## 9. Folosirea

1. **Laptop:** deschide `https://<adresa>/` (sau `http://<IP-Pi>:8000/`), introdu cheia dashboardului.
2. **Asocierea telefonului (o singură dată):** în dashboard, *Asociere* → **Cod pentru telefon**. Pe telefon deschide `<adresa>/companion` și introdu codul (valabil 10 minute). Telefonul arată „Telefon asociat… Ține joystickul apăsat 5 secunde”. În Chrome, *Adaugă pe ecranul de pornire*.
3. **Contactul de încredere:** în dashboard → **Cod pentru contact**; contactul deschide `<adresa>/contact` și introduce codul.
4. **Sesiunea:**
   - ține joystickul 5 s, iar telefonul preia sesiunea singur;
   - apasă *Pornește conversația vocală* (cere microfonul) și *Permite locația*;
   - bifează trimiterea poziției, dacă vrei ca alerta să conțină locația.
5. Când Vesta cere verificarea, răspunde vocal sau apasă **Sunt bine**. Două verificări afișate fără răspuns duc la **ALERT**. **SOS** duce imediat la ALERT.
6. Contactul vede alerta și ultima locație. Persoana închide alerta din telefon, iar alerta rămâne în istoric ca RESOLVED.

Ține pagina telefonului deschisă și ecranul pornit în timpul sesiunii.

## 10. Ce trebuie verificat pe hardware-ul real

- [ ] Apăsare sub 5 s: nimic. 5 s: o activare. 10 s: tot una. Joystick apăsat la pornirea Pi-ului: nicio activare până la eliberare.
- [ ] Sunetul agentului iese prin căștile Razer; microfonul căștilor e folosit.
- [ ] Căștile deconectate în timpul conversației: telefonul arată „Audio deconectat”, iar „Sunt bine” și SOS funcționează.
- [ ] Wi-Fi oprit pe telefon: după ~45 s dashboardul arată telefonul deconectat; la revenire, sesiunea continuă, fără duplicat.
- [ ] Ecran blocat 1–2 minute: ce oprește Chrome (voce, GPS, heartbeat) și dacă la deblocare pagina se resincronizează.
- [ ] `sudo reboot` în timpul unei sesiuni: serviciul pornește singur, sesiunea apare întreruptă, nu se creează alta.
- [ ] Alimentare din baterie externă: ce raportează dashboardul (POWER_OK / LOW_VOLTAGE / POWER_UNSTABLE).

## Depanare

| Simptom | Cauză / soluție |
| --- | --- |
| `systemctl status vesta` arată eroare la pornire | `journalctl -u vesta -n 50`. Mesajele `... is required outside development/test` înseamnă o variabilă lipsă în `/etc/vesta/vesta.env`. |
| Joystickul nu activează nimic | Verifică pinii 11 și 6; în log caută `gpio`. Încearcă un alt fir. `VESTA_GPIO_MODE` trebuie să fie `gpio`. |
| `HardwareMonitorAlreadyRunning` | Mai rulează o instanță (de exemplu pornită manual). `sudo systemctl restart vesta` și nu porni alt uvicorn. |
| Telefonul: „Conexiunea paginii nu este securizată” | Folosește varianta A sau B de la pasul 8. |
| Dashboardul nu se conectează (cere cheia din nou) | Cheia greșită; ia-o cu `sudo grep DASHBOARD /etc/vesta/vesta.env`. |
| „Serviciul vocal indisponibil” | Cheia/agentul ElevenLabs lipsesc sau nu e internet. SOS și „Sunt bine” funcționează oricum. |
| `LOW_VOLTAGE` | Sursa nu e suficientă; folosește sursa oficială 5 V / 3 A. |

Vesta este un prototip demonstrativ: nu apelează 112 și nu trimite SMS. Contactul vede alerta doar pe pagina `/contact` deschisă.
