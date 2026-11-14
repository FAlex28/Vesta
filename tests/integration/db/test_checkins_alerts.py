import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.db.errors import SessionClosed


def _now():
    return datetime.now(timezone.utc)


# Verificare cu răspuns
def test_checkin_confirmed(db, active_session, ev):
    sid = active_session["session_id"]
    created, c = db.checkins.request(sid, round_no=1, attempt_no=1)
    assert created and c["status"] == "REQUESTED"
    db.checkins.mark_delivered(c["checkin_id"], "AUDIO")
    r = db.checkins.confirm(sid, via="VOICE_TOOL", event_id=ev(), checkin_id=c["checkin_id"])
    assert r.status == "CONFIRMED"
    assert r.checkin["confirmed_via"] == "VOICE_TOOL"


def test_checkin_request_is_idempotent(db, active_session):
    sid = active_session["session_id"]
    a = db.checkins.request(sid, round_no=1, attempt_no=1)
    b = db.checkins.request(sid, round_no=1, attempt_no=1)
    assert a[0] is True and b[0] is False
    assert a[1]["checkin_id"] == b[1]["checkin_id"]


# Verificare fără răspuns (timp simulat): livrată vs nelivrată se disting
def test_checkin_timeout_delivered_vs_undelivered(db, owner, ev):
    s1 = db.sessions.activate(owner, ev()).session
    _, c1 = db.checkins.request(s1["session_id"], round_no=1, attempt_no=1)
    db.checkins.mark_delivered(c1["checkin_id"], "AUDIO")
    s2 = db.sessions.activate(owner + "-b", ev()).session
    _, c2 = db.checkins.request(s2["session_id"], round_no=1, attempt_no=1)  # nelivrată

    expired = {r["checkin_id"]: r for r in db.checkins.expire_due(at=_now() + timedelta(seconds=16))}
    assert expired[c1["checkin_id"]]["delivered_at"] is not None
    assert expired[c2["checkin_id"]]["delivered_at"] is None
    tl = db.events.timeline(s1["session_id"])
    timeout_ev = [e for e in tl if e["event_type"] == "CHECKIN_TIMED_OUT"][0]
    assert timeout_ev["metadata"]["was_delivered"] is True
    # rerularea nu dublează evenimentele
    db.checkins.expire_due(at=_now() + timedelta(seconds=20))
    assert len([e for e in db.events.timeline(s1["session_id"]) if e["event_type"] == "CHECKIN_TIMED_OUT"]) == 1


# Audio indisponibil → confirmare alternativă din UI
def test_audio_unavailable_then_ui_confirmation(db, active_session, ev):
    sid = active_session["session_id"]
    _, c = db.checkins.request(sid, round_no=1, attempt_no=1)
    assert db.checkins.mark_audio_unavailable(c["checkin_id"])["status"] == "AUDIO_UNAVAILABLE"
    db.checkins.mark_delivered(c["checkin_id"], "COMPANION_UI")
    r = db.checkins.confirm(sid, via="UI_BUTTON", event_id=ev())
    assert r.status == "CONFIRMED" and r.checkin["delivery_channel"] == "COMPANION_UI"


# Confirmare întârziată: nu modifică nimic
def test_late_confirmation(db, active_session, ev):
    sid = active_session["session_id"]
    _, c = db.checkins.request(sid, round_no=1, attempt_no=1, timeout_s=15)
    r = db.checkins.confirm(sid, via="UI_BUTTON", event_id=ev(), checkin_id=c["checkin_id"],
                            at=_now() + timedelta(seconds=30))
    assert r.status == "LATE"
    assert db.checkins.open_for_session(sid)["status"] == "REQUESTED"


# Confirmare întârziată după alertă: alerta rămâne ACTIVE
def test_confirmation_after_alert_does_not_close_alert(db, active_session, ev):
    sid = active_session["session_id"]
    _, c = db.checkins.request(sid, round_no=1, attempt_no=1)
    db.alerts.raise_alert(sid, "CHECKIN_TIMEOUT", event_id=ev())
    r = db.checkins.confirm(sid, via="VOICE_TOOL", event_id=ev(), checkin_id=c["checkin_id"])
    assert r.status == "NOT_ALLOWED"
    assert db.alerts.active_for_session(sid)["status"] == "ACTIVE"
    assert db.sessions.get(sid)["state"] == "ALERT"


def test_confirmation_for_other_sessions_checkin(db, owner, ev):
    a = db.sessions.activate(owner, ev()).session
    b = db.sessions.activate(owner + "-x", ev()).session
    _, cb = db.checkins.request(b["session_id"], round_no=1, attempt_no=1)
    r = db.checkins.confirm(a["session_id"], via="UI_BUTTON", event_id=ev(), checkin_id=cb["checkin_id"])
    assert r.status == "WRONG_SESSION"
    assert db.checkins.open_for_session(b["session_id"]) is not None


def test_duplicate_confirmation(db, active_session, ev):
    sid, e = active_session["session_id"], ev()
    db.checkins.request(sid, round_no=1, attempt_no=1)
    assert db.checkins.confirm(sid, via="UI_BUTTON", event_id=e).status == "CONFIRMED"
    assert db.checkins.confirm(sid, via="UI_BUTTON", event_id=e).status == "DUPLICATE_EVENT"


# SOS explicit
def test_explicit_sos(db, active_session, ev):
    sid = active_session["session_id"]
    db.locations.record(session_id=sid, lat=45.7537, lon=21.2257, accuracy_m=12, simulated=True)
    r = db.alerts.raise_alert(sid, "SOS_EXPLICIT", event_id=ev(), source="COMPANION",
                              recipients=["contact-1", "contact-2"])
    assert r.status == "CREATED"
    assert r.alert["incident_type"] == "SOS_EXPLICIT"
    assert r.alert["location_snapshot"]["status"] == "CURRENT"
    assert r.alert["location_snapshot"]["simulated"] is True
    assert {n["recipient_id"] for n in r.notifications_queued} == {"contact-1", "contact-2"}
    assert all(n["status"] == "PENDING" for n in r.notifications_queued)


# SOS fără GPS: alerta se creează, locația e UNAVAILABLE (nu se inventează)
def test_sos_without_gps(db, active_session, ev):
    r = db.alerts.raise_alert(active_session["session_id"], "SOS_EXPLICIT", event_id=ev())
    assert r.alert["location_snapshot"] == {"status": "UNAVAILABLE"}


# Alertă duplicată: același event retransmis
def test_duplicate_sos_request(db, active_session, ev):
    sid, e = active_session["session_id"], ev()
    r1 = db.alerts.raise_alert(sid, "SOS_EXPLICIT", event_id=e, recipients=["c"])
    r2 = db.alerts.raise_alert(sid, "SOS_EXPLICIT", event_id=e, recipients=["c"])
    assert r2.status == "DUPLICATE_EVENT" and r2.notifications_queued == []
    assert r2.alert["alert_id"] == r1.alert["alert_id"]
    assert len(db.notifications.for_alert(r1.alert["alert_id"])) == 1


# SOS simultan cu timeout → o singură alertă, două motive, o singură notificare
def test_sos_and_timeout_simultaneous(db, active_session, ev):
    sid, out, barrier = active_session["session_id"], [], threading.Barrier(2)

    def go(kind):
        barrier.wait()
        out.append(db.alerts.raise_alert(sid, kind, event_id=ev(), recipients=["contact-1"]))

    ts = [threading.Thread(target=go, args=(k,)) for k in ("SOS_EXPLICIT", "CHECKIN_TIMEOUT")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(r.status for r in out) == ["CREATED", "MERGED"]
    alerts = db.alerts.list(session_id=sid)
    assert len(alerts) == 1
    assert set(alerts[0]["triggers"]) == {"SOS_EXPLICIT", "CHECKIN_TIMEOUT"}
    assert len(db.notifications.for_alert(alerts[0]["alert_id"])) == 1


# Alertă RESOLVED: păstrată, cu moment de închidere; notificarea originală neatinsă
def test_resolve_alert(db, active_session, ev):
    sid = active_session["session_id"]
    r = db.alerts.raise_alert(sid, "SOS_EXPLICIT", event_id=ev(), recipients=["contact-1"])
    original = r.notifications_queued[0]
    db.notifications.claim_due(50)
    db.notifications.mark_sent(original["notification_id"])
    db.notifications.mark_viewed(original["notification_id"])

    res = db.alerts.resolve(r.alert["alert_id"], event_id=ev(), resolved_by="USER", reason="FALSE_ALARM")
    assert res.status == "RESOLVED"
    assert res.alert["resolved_at"] is not None and res.alert["status"] == "RESOLVED"
    assert [n["kind"] for n in res.notifications_queued] == ["ALERT_RESOLVED"]
    notes = {n["kind"]: n for n in db.notifications.for_alert(r.alert["alert_id"])}
    assert notes["ALERT_RAISED"]["status"] == "VIEWED"
    assert db.sessions.get(sid)["state"] == "CANCELLED"
    assert db.sessions.get(sid)["end_reason"] == "ALERT_RESOLVED"

    # a doua rezolvare și un ALERT vechi retransmis nu reactivează nimic
    assert db.alerts.resolve(r.alert["alert_id"], event_id=ev(), resolved_by="USER",
                             reason="x").status == "ALREADY_RESOLVED"
    with pytest.raises(SessionClosed):
        db.alerts.raise_alert(sid, "SOS_EXPLICIT", event_id=ev())
    assert db.alerts.get(r.alert["alert_id"])["status"] == "RESOLVED"


def test_db_refuses_reopening_resolved_alert(db, active_session, ev):
    r = db.alerts.raise_alert(active_session["session_id"], "SOS_EXPLICIT", event_id=ev())
    db.alerts.resolve(r.alert["alert_id"], event_id=ev(), resolved_by="USER", reason="ok")
    with pytest.raises(Exception, match="cannot be reopened"):
        with db.pool.transaction() as tx:
            tx.exec("UPDATE alerts SET status = 'ACTIVE', resolved_at = NULL WHERE alert_id = %s",
                    (r.alert["alert_id"],))
