"""Verifică rapid Tiger Data cu un scenariu demo complet (date marcate simulated).

    python scripts/tiger_smoke_test.py            # păstrează sesiunea demo (vizibilă în consola Tiger)
    python scripts/tiger_smoke_test.py --cleanup  # o închide și îi șterge locațiile la final
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.db import Database  # noqa: E402

OWNER = "demo-smoke-test"
TIMISOARA = (45.7537, 21.2257)  # Piața Victoriei


def main(cleanup: bool) -> None:
    with Database(max_size=2) as db:
        info = db.ping()
        print(f"Conectat. TimescaleDB {info['timescaledb']} | schema {info['schema']}")
        print(f"Migrări noi: {db.migrate() or 'niciuna'}")

        open_s = db.sessions.get_open(OWNER)
        if open_s and open_s["state"] == "ALERT":
            a = db.alerts.active_for_session(open_s["session_id"])
            db.alerts.resolve(a["alert_id"], event_id=uuid.uuid4(), resolved_by="SMOKE_TEST", reason="cleanup")
        elif open_s:
            db.sessions.cancel(open_s["session_id"], event_id=uuid.uuid4(), reason="SMOKE_TEST_CLEANUP")

        act = db.sessions.activate(OWNER, uuid.uuid4(), simulated=True)
        sid = act.session["session_id"]
        print(f"Activare: {act.status} → sesiune {sid}")
        print(f"Reapăsare: {db.sessions.activate(OWNER, uuid.uuid4()).status}")

        db.locations.record(session_id=sid, lat=TIMISOARA[0], lon=TIMISOARA[1], accuracy_m=12, simulated=True)
        _, c = db.checkins.request(sid, round_no=1, attempt_no=1)
        db.checkins.mark_delivered(c["checkin_id"], "AUDIO")
        print(f"Confirmare: {db.checkins.confirm(sid, via='UI_BUTTON', event_id=uuid.uuid4()).status}")

        r = db.alerts.raise_alert(sid, "SOS_EXPLICIT", event_id=uuid.uuid4(), source="COMPANION",
                                  recipients=["demo-contact"])
        print(f"SOS: {r.status}, notificări în outbox: {len(r.notifications_queued)}, "
              f"locație: {r.alert['location_snapshot']['status']}")
        print(f"Locație curentă: {db.locations.latest(sid)}")
        print(f"Istoric: {[e['event_type'] for e in reversed(db.events.timeline(sid))]}")

        if cleanup:
            db.alerts.resolve(r.alert["alert_id"], event_id=uuid.uuid4(), resolved_by="SMOKE_TEST", reason="cleanup")
            with db.pool.autocommit() as tx:
                tx.exec("DELETE FROM locations WHERE session_id = %s", (sid,))
            print("Alertă RESOLVED, sesiune închisă, locații șterse (istoricul rămâne pentru audit).")


if __name__ == "__main__":
    main(cleanup="--cleanup" in sys.argv)
