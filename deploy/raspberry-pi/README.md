# Vesta pe Raspberry Pi: instalare pas cu pas

Rezultatul: Raspberry Pi 4 rulează tot pe portul 8000, la o adresă HTTPS fixă: backendul, regulile de siguranță, joystickul pe GPIO17, notificările și aplicația web.
- Dispeceratul deschide dashboardul.
- Telefonul persoanei se asociază scanând QR-ul de pe dispozitiv.
- Contactele de urgență primesc invitația prin SMS, chiar de la ea.

## 1. Ce îți trebuie

- Raspberry Pi 4 și card microSD de minimum 16 GB.
- **Sursă oficială 5 V / 3 A USB-C.** O baterie externă slabă produce `LOW_VOLTAGE` și poate opri Pi-ul fără nicio alertă.
- Modul joystick (de tip KY-023, cu pin `SW`) și 2 fire dupont mamă–mamă.
- Telefon Android cu Chrome; căștile Razer împerecheate prin Bluetooth cu telefonul.
- Laptopul cu proiectul (Node.js 20.19+ pentru build).
- Internet pe Pi și pe telefoane (de exemplu hotspotul telefonului). ElevenLabs, notificările push și Tiger Data sunt servicii cloud.
- Pentru folosire reală: un domeniu propriu (pentru adresa HTTPS fixă, pasul 8).
- Opțional: cheie ElevenLabs + agent; URL Tiger Data; o imprimantă pentru eticheta QR.

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
tar -czf vesta.tgz --exclude=node_modules --exclude=.venv --exclude=.git --exclude=__pycache__ --exclude=.state backend deploy shared frontend/dist
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

Ce face scriptul:
- instalează Python, `gpiozero`, `lgpio` și `qrencode`;
- creează `/etc/vesta/vesta.env` cu secrete generate aleator și **codul de asociere al dispozitivului**;
- instalează serviciul `vesta`, care pornește singur la boot și repornește la eroare;
- la final afișează **cheia dashboardului** (pentru dispecerat). Păstreaz-o privat.

## 6. Configurarea

```bash
sudo nano /etc/vesta/vesta.env
```

- **Voce:** `VESTA_VOICE_MODE=elevenlabs`, `ELEVENLABS_API_KEY=...` și `ELEVENLABS_AGENT_ID=...`. Fără ele, pune `VESTA_VOICE_MODE=demo` (text în loc de voce).
  Agentul ElevenLabs se creează o dată, conform `backend/docs/ELEVENLABS.md`:
  - limba română;
  - promptul din `backend/app/voice/prompt.py`;
  - client tools: `vesta_confirm_safe`, `vesta_request_help` și `vesta_report_technical_issue` (parametru text `issue`);
  - autentificare activată (agent privat).
- **Notificări:** `VESTA_VAPID_SUBJECT=mailto:adresa-echipei@...`. Cheile push se generează singure în `/var/lib/vesta`.
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
- ținut **5 secunde** continuu, apare o singură activare (în dispecerat: ACTIVE);
- ținut 10 secunde, tot o singură activare;
- o nouă activare cere eliberare și apăsare din nou.

## 8. Adresa HTTPS fixă (obligatorie pentru microfon, GPS, QR și notificări)

Chrome pe Android permite microfonul, locația și notificările doar pe HTTPS. Adresa trebuie să rămână **aceeași**, pentru că apare:
- în QR-ul de pe dispozitiv;
- în SMS-urile trimise contactelor;
- în abonamentele la notificări.

**A. Tunel Cloudflare cu nume (recomandat).** Ai nevoie de un cont Cloudflare gratuit și un domeniu propriu, de exemplu `vesta-echipa.ro`.

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o cloudflared
sudo install cloudflared /usr/local/bin/cloudflared
cloudflared tunnel login                          # deschide linkul afișat și alege domeniul
cloudflared tunnel create vesta                   # afișează <id-tunel>
cloudflared tunnel route dns vesta vesta.domeniul-tau.ro
```

Creează `~/.cloudflared/config.yml`:

```yaml
tunnel: vesta
credentials-file: /home/<utilizator>/.cloudflared/<id-tunel>.json
ingress:
  - hostname: vesta.domeniul-tau.ro
    service: http://127.0.0.1:8000
  - service: http_status:404
```

Apoi:

```bash
sudo cloudflared --config ~/.cloudflared/config.yml service install   # pornește tunelul la boot
sudo nano /etc/vesta/vesta.env      # VESTA_PUBLIC_URL=https://vesta.domeniul-tau.ro
sudo systemctl restart vesta
```

**B. Tunel rapid (doar pentru teste).** Adresa `https://….trycloudflare.com` se schimbă la fiecare repornire, deci QR-ul și invitațiile trebuie refăcute de fiecare dată.

```bash
sed -e "s#__USER__#$(id -un)#g" ~/Vesta/deploy/raspberry-pi/vesta-tunnel.service | sudo tee /etc/systemd/system/vesta-tunnel.service
sudo systemctl daemon-reload && sudo systemctl enable --now vesta-tunnel
journalctl -u vesta-tunnel | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1
```

**C. Doar în rețeaua locală, fără notificări.** Pe telefon, în Chrome: `chrome://flags` → *Insecure origins treated as secure* → `http://<IP-ul Pi-ului>:8000` → *Enabled* → *Relaunch*. Microfonul și GPS-ul merg; notificările push nu.

## 9. Eticheta QR a dispozitivului

```bash
bash ~/Vesta/deploy/raspberry-pi/show-pairing-qr.sh
```

Scriptul:
- afișează QR-ul în terminal și codul de 10 caractere;
- salvează imaginea `~/vesta-pairing-qr.png`.

Tipărește imaginea, cu codul sub ea, și lipește eticheta pe carcasa Vesta. Codul funcționează ca parola de pe un router: oricine îl are poate asocia un telefon. De aceea eticheta trebuie să ajungă doar la persoana protejată.

Dacă eticheta se pierde, rulează `bash ~/Vesta/deploy/raspberry-pi/rotate-pairing-code.sh`. Toate telefoanele trebuie apoi să scaneze noul QR; contactele rămân.

## 10. Folosirea

**Persoana protejată, pe telefonul ei (o singură dată):**

1. Scanează QR-ul de pe dispozitiv cu camera. Se deschide Vesta și apare „Telefon asociat”. În Chrome alege *Adaugă pe ecranul de pornire*.
2. La *Contacte de urgență* scrie numele tău, apoi numele și numărul contactului. Apasă **Adaugă și pregătește invitația**, apoi **Trimite prin SMS**. Se deschide aplicația de mesaje cu textul gata; apasă Trimite.

**Contactul de urgență (o singură dată):** deschide linkul din SMS și apasă **Permite notificările**. De acum primește alerta pe telefon chiar cu pagina închisă.

**Dispeceratul (laptop):** deschide adresa HTTPS și introdu cheia dashboardului (`sudo grep DASHBOARD /etc/vesta/vesta.env`).
- Dispeceratul doar monitorizează și nu împarte coduri.
- În timpul unei alerte active vede ultima locație autorizată.
- **Mod prezentare** ascunde coordonatele când ecranul e proiectat.

**O sesiune:**

1. Ține joystickul **5 secunde**; telefonul asociat preia sesiunea singur.
2. Pe telefon:
   - apasă *Pornește conversația vocală* (cere microfonul);
   - apasă *Permite locația*;
   - bifează trimiterea poziției, dacă vrei ca alerta să conțină locația.
3. Când Vesta întreabă dacă ești bine, răspunde vocal sau apasă **Sunt bine**.
   - Două verificări afișate fără răspuns duc la **ALERT**.
   - **SOS** duce imediat la ALERT.
4. Contactele primesc notificarea și văd alerta cu ultima locație.
5. Persoana închide alerta din telefon. Alerta rămâne în istoric ca RESOLVED, iar contactele primesc „Alerta a fost închisă”.

Ține pagina telefonului deschisă și ecranul pornit în timpul sesiunii.

## 11. Ce trebuie verificat pe hardware-ul real

- [ ] Apăsare sub 5 s: nimic. 5 s: o activare. 10 s: tot una. Joystick apăsat la pornirea Pi-ului: nicio activare până la eliberare.
- [ ] QR-ul de pe etichetă asociază telefonul; un al doilea telefon apare în lista „Telefoane asociate”.
- [ ] SMS-ul de invitație ajunge, linkul deschide pagina contactului, notificările se activează.
- [ ] Cu pagina contactului **închisă**, un SOS aduce notificarea pe telefonul contactului.
- [ ] Sunetul agentului iese prin căștile Razer; microfonul căștilor e folosit.
- [ ] Căștile deconectate în timpul conversației: telefonul arată „Audio deconectat”, iar „Sunt bine” și SOS funcționează.
- [ ] Wi-Fi oprit pe telefon: după ~45 s dispeceratul arată telefonul deconectat; la revenire, sesiunea continuă, fără duplicat.
- [ ] Ecran blocat 1–2 minute: ce oprește Chrome (voce, GPS, heartbeat) și dacă la deblocare pagina se resincronizează.
- [ ] `sudo reboot` în timpul unei sesiuni: serviciul pornește singur, sesiunea apare întreruptă, telefonul rămâne asociat, contactele rămân.
- [ ] Alimentare din baterie externă: ce raportează dispeceratul (POWER_OK / LOW_VOLTAGE / POWER_UNSTABLE).

## Depanare

| Simptom | Cauză / soluție |
| --- | --- |
| `systemctl status vesta` arată eroare la pornire | `journalctl -u vesta -n 50`. Mesajele `... is required outside development/test` înseamnă o variabilă lipsă în `/etc/vesta/vesta.env`. |
| Joystickul nu activează nimic | Verifică pinii 11 și 6; în log caută `gpio`. `VESTA_GPIO_MODE` trebuie să fie `gpio`. |
| `HardwareMonitorAlreadyRunning` | Mai rulează o instanță (de exemplu pornită manual). `sudo systemctl restart vesta` și nu porni alt uvicorn. |
| Telefonul: „Conexiunea paginii nu este securizată” | Folosește adresa HTTPS de la pasul 8. |
| „Codul nu se potrivește cu dispozitivul” | Codul de pe etichetă diferă de `VESTA_DEVICE_PAIRING_CODE`; rulează din nou `show-pairing-qr.sh` și retipărește eticheta. |
| „Dispozitivul are deja numărul maxim de telefoane” | Elimină un telefon din *Telefoane asociate* sau mărește `VESTA_MAX_PAIRED_PHONES`. |
| Contactul nu primește notificări | Pagina trebuie deschisă pe HTTPS și **Permite notificările** apăsat; în Chrome, notificările site-ului nu trebuie blocate. |
| Dispeceratul cere cheia din nou | Cheia e greșită; ia-o cu `sudo grep DASHBOARD /etc/vesta/vesta.env`. |
| „Serviciul vocal indisponibil” | Cheia/agentul ElevenLabs lipsesc sau nu e internet. SOS și „Sunt bine” funcționează oricum. |
| `LOW_VOLTAGE` | Sursa nu e suficientă; folosește sursa oficială 5 V / 3 A. |

Vesta este un prototip demonstrativ: nu apelează 112 și nu trimite SMS de pe server. Invitațiile pleacă prin SMS din telefonul persoanei; alertele ajung la contacte ca notificări push.
