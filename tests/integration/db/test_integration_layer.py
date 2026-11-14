from datetime import datetime, timedelta, timezone

import pytest

from app.db.errors import Forbidden, InvalidLocation, Unauthorized
from app.integrations.outbox import OutboxDispatcher
from app.integrations.power import PowerMonitor
from app.integrations.startup import recover_on_startup


def _now():
    return datetime.now(timezone.utc)


# ---------- GPS ----------

def test_gps_unavailable_does_not_block(db, active_session):
    assert db.locations.latest(active_session["session_id"]) == {"status": "UNAVAILABLE"}


def test_gps_current_stale_and_low_accuracy(db, active_session):
    sid = active_session["session_id"]
    db.locations.record(session_id=sid, lat=45.75, lon=21.22, accuracy_m=250,
                        occurred_at=_now() - timedelta(seconds=10))
    cur = db.locations.latest(sid)
    assert cur["status"] == "CURRENT" and cur["low_accuracy"] is True and cur["simulated"] is False
    stale = db.locations.latest(sid, at=_now() + timedelta(seconds=120))
    assert stale["status"] == "STALE" and stale["age_s"] > 60


@pytest.mark.parametrize("bad", [
    {"lat": 91, "lon": 0}, {"lat": 0, "lon": 181}, {"lat": "x", "lon": 0},
    {"lat": float("nan"), "lon": 0}, {"lat": 0, "lon": 0, "accuracy_m": -1},
    {"lat": 0, "lon": 0, "occurred_at": datetime.now(timezone.utc) + timedelta(hours=1)},
])
def test_gps_validation(db, active_session, bad):
    with pytest.raises(InvalidLocation):
        db.locations.record(session_id=active_session["session_id"], **bad)


# ---------- heartbeat / telefon ----------

def test_phone_disconnect_and_reconnect(db, owner, active_session):
    sid, client = active_session["session_id"], f"phone-{owner}"
    first = db.companions.heartbeat(client, owner, session_id=sid, visibility="VISIBLE")
    assert first["previous_state"] is None

    mine = lambda rows: [r for r in rows if r["client_id"] == client]  # noqa: E731
    assert mine(db.companions.sweep(at=_now() + timedelta(seconds=10))) == []
    r = mine(db.companions.sweep(at=_now() + timedelta(seconds=35)))
    assert r[0]["connection_state"] == "RECONNECTING"
    r = mine(db.companions.sweep(at=_now() + timedelta(seconds=50)))
    assert r[0]["connection_state"] == "DISCONNECTED"
    assert mine(db.companions.sweep(at=_now() + timedelta(seconds=90))) == []  # o singură dată

    back = db.companions.heartbeat(client, owner, session_id=sid, visibility="VISIBLE")
    assert back["reconnected"] is True and back["client"]["last_reconnect_at"] is not None

    types = [e["event_type"] for e in db.events.timeline(sid)]
    assert types.count("PHONE_DISCONNECTED") == 1 and types.count("PHONE_RECONNECTED") == 1
    # pierderea conexiunii NU generează alertă / SOS
    assert db.alerts.active_for_session(sid) is None
    assert db.sessions.get(sid)["state"] == "ACTIVE"


def test_page_hidden_and_visible(db, owner, active_session):
    sid, client = active_session["session_id"], f"phone-{owner}"
    db.companions.heartbeat(client, owner, session_id=sid, visibility="VISIBLE")
    assert db.companions.heartbeat(client, owner, visibility="HIDDEN")["visibility_changed"] is True
    assert db.companions.heartbeat(client, owner, visibility="VISIBLE")["visibility_changed"] is True
    types = [e["event_type"] for e in db.events.timeline(sid)]
    assert "PAGE_HIDDEN" in types and "PAGE_VISIBLE" in types


# ---------- evenimente tehnice: audio / ElevenLabs ----------

def test_voice_service_events_deduplicated(db, active_session, ev):
    sid, e = active_session["session_id"], ev()
    assert db.events.log_technical_event(component="VOICE_SERVICE", event_type="SERVICE_UNAVAILABLE",
                                         event_id=e, session_id=sid, metadata={"error": "timeout"})
    assert not db.events.log_technical_event(component="VOICE_SERVICE", event_type="SERVICE_UNAVAILABLE",
                                             event_id=e, session_id=sid)
    db.events.log_technical_event(component="AUDIO", event_type="AUDIO_DISCONNECTED", session_id=sid)
    types = [x["event_type"] for x in db.events.timeline(sid)]
    assert types.count("SERVICE_UNAVAILABLE") == 1 and "AUDIO_DISCONNECTED" in types
    assert db.sessions.get(sid)["state"] == "ACTIVE"  # sesiunea continuă în mod degradat


# ---------- outbox ----------

def test_outbox_retries_then_gives_up(db, active_session, ev):
    r = db.alerts.raise_alert(active_session["session_id"], "SOS_EXPLICIT", event_id=ev(), recipients=["c"])
    nid = r.notifications_queued[0]["notification_id"]

    def failing(n):
        if n["notification_id"] == nid:
            raise ConnectionError("contact page offline")

    d = OutboxDispatcher(db, failing, batch=50)
    for _ in range(5):  # max_attempts=3 în setările de test, backoff 0
        d.run_once()
    n = [x for x in db.notifications.for_alert(r.alert["alert_id"])][0]
    assert n["status"] == "GAVE_UP" and n["attempt_count"] == 3 and n["delivered_at"] is None


def test_outbox_sent_is_not_delivered_until_ack(db, active_session, ev):
    r = db.alerts.raise_alert(active_session["session_id"], "SOS_EXPLICIT", event_id=ev(), recipients=["c"])
    nid = r.notifications_queued[0]["notification_id"]
    OutboxDispatcher(db, lambda n: None, batch=50).run_once()
    n = db.notifications.for_alert(r.alert["alert_id"])[0]
    assert n["status"] == "SENT" and n["delivered_at"] is None
    db.notifications.mark_delivered(nid)
    db.notifications.mark_viewed(nid)
    assert db.notifications.mark_delivered(nid) is None  # nu retrogradează VIEWED
    assert db.notifications.for_alert(r.alert["alert_id"])[0]["status"] == "VIEWED"


# ---------- alimentare ----------

def test_power_monitor_logs_only_changes(db):
    values = iter(["throttled=0x0", "throttled=0x0", "throttled=0x50005", "throttled=0x50000"])
    pm = PowerMonitor(db, reader=lambda: next(values))
    states = [pm.run_once().state for _ in range(4)]
    assert states == ["POWER_OK", "POWER_OK", "LOW_VOLTAGE", "POWER_UNSTABLE"]
    logged = [e["event_type"] for e in db.events.technical(component="POWER")]
    assert sorted(logged) == ["LOW_VOLTAGE", "POWER_OK", "POWER_UNSTABLE"]


# ---------- acces neautorizat ----------

def test_tokens_and_authorization(db, owner, ev, active_session):
    sid = active_session["session_id"]
    other = db.sessions.activate(owner + "-other", ev()).session
    user = db.tokens.issue("USER", owner)
    contact = db.tokens.issue("CONTACT", owner, session_id=sid)
    demo = db.tokens.issue("PUBLIC_DEMO", "demo")

    assert db.tokens.authorize(user, sid, "control")["role"] == "USER"
    assert db.tokens.authorize(contact, sid, "read_location")["role"] == "CONTACT"
    with pytest.raises(Forbidden):
        db.tokens.authorize(contact, sid, "control")                      # contactul nu controlează
    with pytest.raises(Forbidden):
        db.tokens.authorize(user, other["session_id"], "read_location")  # ID schimbat în URL
    with pytest.raises(Forbidden):
        db.tokens.authorize(contact, other["session_id"], "read_location")
    with pytest.raises(Forbidden):
        db.tokens.authorize(demo, sid, "read_location")                   # sesiune reală pe dashboard public
    for bad in (None, "", "not-a-token"):
        with pytest.raises(Unauthorized):
            db.tokens.authorize(bad, sid, "read_location")
    expired = db.tokens.issue("USER", owner, ttl_minutes=-1)
    with pytest.raises(Unauthorized):
        db.tokens.verify(expired)
    db.tokens.revoke(user)
    with pytest.raises(Unauthorized):
        db.tokens.verify(user)
    # tokenul nu e stocat în clar
    with db.pool.autocommit() as tx:
        assert tx.one("SELECT count(*) AS n FROM access_tokens WHERE token_hash = convert_to(%s, 'UTF8')",
                      (contact,))["n"] == 0


def test_demo_session_visible_to_public_demo(db, ev):
    s = db.sessions.activate("demo-owner-" + str(ev())[:6], ev(), simulated=True).session
    demo = db.tokens.issue("PUBLIC_DEMO", "demo")
    assert db.tokens.authorize(demo, s["session_id"], "read_location")


# ---------- restart Raspberry Pi ----------

def test_restart_recovery(fresh_db, ev):
    db = fresh_db
    normal = db.sessions.activate("u1", ev()).session
    unconfirmed = db.sessions.activate("u2", ev()).session
    db.checkins.request(unconfirmed["session_id"], round_no=1, attempt_no=1)
    db.sessions.transition(unconfirmed["session_id"], "UNCONFIRMED", event_id=ev())
    alerted = db.sessions.activate("u3", ev()).session
    a = db.alerts.raise_alert(alerted["session_id"], "SOS_EXPLICIT", event_id=ev(), recipients=["c1", "c2"])
    claimed = db.notifications.claim_due(1)  # una „în zbor” la momentul restartului
    db.notifications.mark_sent(db.notifications.claim_due(1)[0]["notification_id"])  # una deja trimisă
    db.companions.heartbeat("p1", "u1", session_id=normal["session_id"])

    summary = recover_on_startup(db)
    assert {s["session_id"] for s in summary["interrupted"]} == {normal["session_id"], unconfirmed["session_id"]}
    assert {s["previous_state"] for s in summary["interrupted"]} == {"ACTIVE", "UNCONFIRMED"}
    assert [s["session_id"] for s in summary["still_in_alert"]] == [alerted["session_id"]]
    assert summary["notifications_requeued"] == 1
    assert db.alerts.get(a.alert["alert_id"])["status"] == "ACTIVE"
    assert db.checkins.open_for_session(unconfirmed["session_id"]) is None
    assert db.companions.get("p1")["connection_state"] == "RECONNECTING"
    notes = {n["notification_id"]: n["status"] for n in db.notifications.for_alert(a.alert["alert_id"])}
    assert notes[claimed[0]["notification_id"]] == "FAILED"
    assert "SENT" in notes.values()

    # al doilea restart: nimic nou, nicio sesiune creată
    again = recover_on_startup(db)
    assert again["interrupted"] == [] and again["notifications_requeued"] == 0
    assert len(db.sessions.list_recent()) == 3
    # utilizatoarea poate porni o sesiune nouă după întrerupere
    assert db.sessions.activate("u1", ev()).status == "CREATED"
    stats = {r["event_type"]: r["events"] for r in db.events.daily_stats()}
    assert stats["SESSION_ACTIVATED"] == 4 and stats["SESSION_INTERRUPTED"] == 2
