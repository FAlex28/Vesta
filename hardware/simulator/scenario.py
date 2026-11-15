"""Scenariul complet pe hardware simulat: joystick -> backend -> Nano, cu verificări.

Rulează cu backendul pornit în modul demo (telefonul e jucat de script, ca pagina /companion):
  VESTA_OPEN_ACCESS=true, verificări rapide (de ex. VESTA_FIRST_CHECKIN_DELAY_SECONDS=3,
  VESTA_CHECKIN_RESPONSE_TIMEOUT_SECONDS=5, VESTA_CHECKIN_MAX_ATTEMPTS=1)
și cu `vesta_sim.py` pornit. Apoi:

  python hardware/simulator/scenario.py [--backend http://127.0.0.1:8000] [--sim http://127.0.0.1:8790]

Nu atinge backendul altfel decât ar face-o dispozitivul și telefonul.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import uuid

results: list[tuple[str, bool, str]] = []


def http(method: str, url: str, body: dict | None = None, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read() or b"{}")


class Rig:
    def __init__(self, backend: str, sim: str) -> None:
        self.backend, self.sim = backend.rstrip("/"), sim.rstrip("/")
        self.phone_token: str | None = None
        self.control_token: str | None = None
        self.session_id: str | None = None

    # -- hardware ---------------------------------------------------------------------
    def nano(self) -> dict:
        return http("GET", self.sim + "/state")["nano"]

    def sim_post(self, path: str) -> dict:
        return http("POST", self.sim + path)

    def hold(self, seconds: float) -> None:
        self.sim_post(f"/gpio17/hold?seconds={seconds}")
        time.sleep(seconds + 0.3)

    # -- backend, ca dispeceratul -----------------------------------------------------------
    def status(self) -> dict:
        return http("GET", self.backend + "/api/v1/status")

    def state(self) -> str:
        return self.status()["system_state"]

    def current(self) -> dict:
        """Sesiunea curentă a dispozitivului (statusul listează și sesiunile încheiate)."""
        sessions = self.status()["sessions"]
        open_ = [s for s in sessions if s["state"] in ("ACTIVE", "CHECK_IN", "UNCONFIRMED", "ALERT")]
        return max(open_ or sessions, key=lambda s: s["created_at"])

    # -- telefonul (ca pagina /companion în modul demo) -----------------------------------
    def phone_claim(self) -> None:
        if self.phone_token is None:
            self.phone_token = http("POST", self.backend + "/api/v1/access/open", {"scope": "companion"})["access_token"]
        claimed = http("GET", self.backend + "/api/v1/companion/session", token=self.phone_token)
        self.session_id = claimed["session"]["id"] if claimed["session"] else None
        self.control_token = claimed["control_token"]

    def phone(self, action: str, body: dict) -> dict:
        body = {"event_id": f"scenario-{uuid.uuid4().hex[:10]}", **body}
        return http("POST", f"{self.backend}/api/v1/sessions/{self.session_id}/{action}", body, self.control_token)

    def resolve(self) -> None:
        alert = self.current()["alert"]
        self.phone(f"alerts/{alert['id']}/resolve", {"action": "USER_CONFIRMED_SAFE"})


def wait(fn, timeout: float = 15.0, every: float = 0.3):
    end = time.monotonic() + timeout
    value = None
    while time.monotonic() < end:
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - backend oprit în pasul de offline
            value = exc
        if value and not isinstance(value, Exception):
            return value
        time.sleep(every)
    return value if not isinstance(value, Exception) else None


def check(name: str, ok, detail: object = "") -> None:
    results.append((name, bool(ok), str(detail)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   [{detail}]"), flush=True)


def nano_is(rig: Rig, led: str, line2: str | None = None, buzzer: str | None = None, timeout: float = 15.0):
    def match():
        v = rig.nano()
        ok = v["led"] == led and (line2 is None or v["lcd"][1].strip() == line2) and (
            buzzer is None or v["buzzer"].startswith(buzzer)
        )
        return v if ok else None

    return wait(match, timeout) or rig.nano()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--sim", default="http://127.0.0.1:8790")
    args = parser.parse_args()
    rig = Rig(args.backend, args.sim)

    # 0. pornire
    v = nano_is(rig, "ALB", "Fara sesiune")
    check("Nano conectat la bridge, standby", v["lcd"][1] == "Fara sesiune" and v["pc_linked"], v)
    check("backend IDLE", rig.state() == "IDLE", rig.state())

    # 1. apăsare scurtă: nimic
    rig.hold(2.0)
    time.sleep(1.5)
    check("apăsare de 2 s ignorată (fără sesiune)", rig.state() == "IDLE", rig.state())

    # 2. ținere 5 s: activare prin /api/v1/device/events (calea reală a Pi-ului)
    rig.hold(5.5)
    check("ținere 5 s -> sesiune ACTIVE", wait(lambda: rig.state() == "ACTIVE"), rig.state())
    session = rig.current()
    check("sesiunea aparține dispozitivului (device_id-ul Pi-ului)", session["device_id"] == http("GET", rig.sim + "/state")["pi"]["device_id"], session["device_id"])
    v = nano_is(rig, "VERDE", "Conversatie OK")
    check("Nano: LED VERDE, LCD VERDE 01 ACTIV", v["led"] == "VERDE" and v["lcd"][0] == "VERDE 01 ACTIV", v)

    # 3. a doua ținere: tot o singură sesiune
    rig.hold(5.5)
    time.sleep(1)
    check("a doua ținere nu creează altă sesiune", len([s for s in rig.status()["sessions"] if s["state"] != "CANCELLED"]) == 1)

    # 4. telefonul primește verificările dar nu răspunde -> alertă CHECKIN_TIMEOUT
    rig.phone_claim()
    delivered: set[str] = set()
    saw_check = None

    def phone_receives_but_stays_silent():
        nonlocal saw_check
        http(
            "POST", f"{rig.backend}/api/v1/sessions/{rig.session_id}/heartbeat",
            {"page_visibility": "VISIBLE"}, rig.control_token)
        s = rig.current()
        cid = s.get("checkin_id")
        if cid and cid not in delivered and s.get("checkin_status") == "REQUESTED":
            rig.phone(f"checkins/{cid}/delivery", {"status": "DELIVERED", "channel": "voice"})
            delivered.add(cid)
        if saw_check is None and rig.nano()["led"] == "GALBEN":
            saw_check = rig.nano()
        return s["state"] == "ALERT"

    wait(phone_receives_but_stays_silent, 45, every=0.5)
    check("verificare livrată: Nano GALBEN 02 CHECK", saw_check and saw_check["lcd"][0] == "GALBEN 02 CHECK", saw_check)
    v = nano_is(rig, "ROSU", "FARA CONFIRMARE", "D7")
    check("fără răspuns: Nano ROSU + buzzer D7, FARA CONFIRMARE", v["led"] == "ROSU" and v["buzzer"].startswith("D7"), v)
    check("backend în ALERT", rig.state() == "ALERT", rig.state())
    # locația se arată dispeceratului doar în timpul unei alerte (confidențialitate)
    sid = rig.current()["id"]
    points = wait(lambda: http("GET", f"{rig.backend}/api/v1/sessions/{sid}/locations")["points"], 8) or []
    check("ruta din GPS-ul Pi-ului e la dispecerat (sursa device_gps)",
          points and all(p["source"] == "device_gps" for p in points), points[:2])

    # 5. telefonul confirmă că e bine -> alerta se închide pe placă
    rig.phone_claim()
    rig.resolve()
    v = nano_is(rig, "ALB", "Alerta rezolvata")
    check("rezolvare: Nano ALB, 'Alerta rezolvata', alarma oprită", v["lcd"][1] == "Alerta rezolvata" and not v["buzzer"].startswith("D7"), v)
    v = nano_is(rig, "ALB", "Fara sesiune", timeout=10)
    check("după 5 s: 'Fara sesiune'", v["lcd"][1] == "Fara sesiune", v)

    # 6. SOS explicit de pe telefon
    rig.hold(5.5)
    wait(lambda: rig.state() == "ACTIVE")
    rig.phone_claim()
    rig.phone("sos", {"source": "android"})
    v = nano_is(rig, "ROSU", "AJUTOR SOLICITAT", "D7")
    check("SOS: Nano ROSU, AJUTOR SOLICITAT, D7", v["lcd"][1] == "AJUTOR SOLICITAT", v)

    # 7. cablul USB al Nano-ului scos în timpul alertei, apoi pus la loc
    rig.sim_post("/nano/usb?connected=0")
    v = wait(lambda: (lambda n: n if n["led"] == "ALBASTRU" or (n["led"] == "ROSU" and not n["pc_linked"]) else None)(rig.nano()), 15)
    check("USB scos: alerta continuă să sune pe placă (fără PC)", v and v["led"] == "ROSU" and v["buzzer"].startswith("D7"), v)
    rig.sim_post("/nano/usb?connected=1")
    v = wait(lambda: (lambda n: n if n["serial_open"] and n["pc_linked"] and n["uptime_ms"] > 2000
                      and n["lcd"][1] == "AJUTOR SOLICITAT" else None)(rig.nano()), 20) or rig.nano()
    check("USB pus la loc: bridge-ul resincronizează alerta după reset", v["pc_linked"] and v["lcd"][1] == "AJUTOR SOLICITAT", v)

    rig.resolve()
    nano_is(rig, "ALB", "Alerta rezolvata")

    # 8. problemă tehnică raportată de telefon -> LED albastru
    rig.hold(5.5)
    wait(lambda: rig.state() == "ACTIVE")
    rig.phone_claim()
    rig.phone("technical-status", {"issue": "microphone_denied", "active": True})
    v = nano_is(rig, "ALBASTRU", "MICROFON", "D8")
    check("microfon refuzat: Nano ALBASTRU 04 ERR / MICROFON, D8", v["lcd"][1] == "MICROFON", v)
    rig.phone("technical-status", {"issue": "microphone_denied", "active": False})
    v = nano_is(rig, "VERDE")
    check("problema rezolvată: Nano revine la VERDE", v["led"] == "VERDE", v)
    rig.phone("cancel", {"reason": "USER_CANCELLED"})
    v = nano_is(rig, "ALB", "Fara sesiune")
    check("sesiune încheiată: Nano standby", v["lcd"][1] == "Fara sesiune", v)

    # 9. fără telefon: verificarea nu poate fi livrată -> problemă tehnică, nu alertă
    rig.hold(5.5)
    wait(lambda: rig.state() == "ACTIVE")
    v = nano_is(rig, "ALBASTRU", timeout=30)
    check("fără telefon: Nano ALBASTRU (verificare nelivrată), fără alertă falsă",
          v["led"] == "ALBASTRU" and rig.state() != "ALERT", (v, rig.state()))
    rig.phone_claim()
    rig.phone("cancel", {"reason": "USER_CANCELLED"})
    nano_is(rig, "ALB", "Fara sesiune")

    # 10. GPS fără fix: Pi-ul nu mai trimite poziții, fără erori pe placă
    rig.sim_post("/gps/fix?on=0")
    time.sleep(3)
    check("GPS fără fix nu aprinde eroare pe Nano", rig.nano()["led"] == "ALB")
    rig.sim_post("/gps/fix?on=1")

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} verificări trecute")
    return 1 if failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
