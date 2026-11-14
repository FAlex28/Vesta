"""SessionManager-ul real (Safety Engine în memorie) + TigerRecorder → Tiger Data (schemă de test).
Timpul e simulat prin parametrul `now`, deci nu așteptăm efectiv timeouturile."""

import asyncio
from datetime import UTC, datetime, timedelta

from app.integrations.persistence import TigerRecorder
from app.realtime.broker import EventBroker
from app.safety import SafetyEngine, SafetyPolicy
from app.security import ControlTokenService
from app.services.session_manager import SessionManager

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def _manager(db):
    policy = SafetyPolicy(check_in_interval=timedelta(seconds=60), response_timeout=timedelta(seconds=15),
                          retry_delay=timedelta(seconds=5), max_attempts=2)
    recorder = TigerRecorder(db, recipients=("contact-1",), response_timeout_s=15)
    mgr = SessionManager(
        engine=SafetyEngine(policy), broker=EventBroker(),
        token_service=ControlTokenService("x" * 48, default_ttl=timedelta(hours=1)),
        control_token_ttl_seconds=3600, scheduler_tick_seconds=0.1,
        client_heartbeat_timeout_seconds=20, recorder=recorder,
    )
    return mgr, recorder


def _types(db, sid):
    return [e["event_type"] for e in reversed(db.events.timeline(sid))]


def test_full_flow_checkin_missed_alert_then_cancel_resolves(db, owner):
    async def scenario():
        mgr, rec = _manager(db)
        await rec.start()
        act = await mgr.activate(event_id="press-1", device_id=owner, now=T0)
        sid, tok = act.session.session_id, act.control_token
        await mgr.activate(event_id="press-1", device_id=owner, now=T0)            # retransmis

        await mgr.advance_all(now=T0 + timedelta(seconds=61))                      # CHECK_IN #1
        await mgr.confirm_safe(sid, token=tok, event_id="ok-1", source="android",
                               now=T0 + timedelta(seconds=65))                     # confirmat
        await mgr.advance_all(now=T0 + timedelta(seconds=126))                     # CHECK_IN #1 (runda 2)
        await mgr.advance_all(now=T0 + timedelta(seconds=142))                     # ratat → UNCONFIRMED
        await mgr.advance_all(now=T0 + timedelta(seconds=148))                     # CHECK_IN #2
        await mgr.advance_all(now=T0 + timedelta(seconds=164))                     # ratat → ALERT
        await mgr.cancel(sid, token=tok, event_id="cancel-1", reason="Am ajuns acasă",
                         now=T0 + timedelta(seconds=200))
        await rec.flush()
        await rec.stop()
        return sid, rec

    sid, rec = asyncio.run(scenario())
    assert rec.failed == 0 and rec.dropped == 0

    s = db.sessions.get(sid)
    assert s["owner_id"] == owner and s["state"] == "CANCELLED" and s["end_reason"] == "ALERT_RESOLVED"
    assert len(db.sessions.list_recent(owner)) == 1

    checkins = db.checkins.for_session(sid)
    assert [(c["round_no"], c["attempt_no"], c["status"]) for c in checkins] == [
        (1, 1, "CONFIRMED"), (2, 1, "TIMED_OUT"), (2, 2, "TIMED_OUT")]

    [alert] = db.alerts.list(session_id=sid)
    assert alert["incident_type"] == "CHECKIN_TIMEOUT" and alert["status"] == "RESOLVED"
    assert alert["resolution_reason"] == "Am ajuns acasă"
    kinds = sorted(n["kind"] for n in db.notifications.for_alert(alert["alert_id"]))
    assert kinds == ["ALERT_RAISED", "ALERT_RESOLVED"]

    types = _types(db, sid)
    assert types[0] == "SESSION_ACTIVATED" and types.count("SESSION_ACTIVATED") == 1
    assert types.count("CHECKIN_TIMED_OUT") == 2
    assert "ALERT_RAISED" in types and types[-1] == "ALERT_RESOLVED"


def test_sos_quick_cancel_and_technical_issues(db, owner):
    async def scenario():
        mgr, rec = _manager(db)
        await rec.start()
        a = await mgr.activate(event_id="p-a", device_id=owner, now=T0)
        await mgr.cancel(a.session.session_id, token=a.control_token, event_id="c-a",
                         reason="ACCIDENTAL_ACTIVATION", now=T0 + timedelta(seconds=2))

        b = await mgr.activate(event_id="p-b", device_id=owner, now=T0 + timedelta(seconds=10))
        sid, tok = b.session.session_id, b.control_token
        from app.safety import TechnicalIssue
        await mgr.set_technical_issue(sid, issue=TechnicalIssue.VOICE_UNAVAILABLE, active=True,
                                      event_id="v-1", source="voice_gateway", token=tok,
                                      now=T0 + timedelta(seconds=11))
        await mgr.request_sos(sid, token=tok, event_id="sos-1", source="android", reason=None,
                              now=T0 + timedelta(seconds=12))
        await mgr.request_sos(sid, token=tok, event_id="sos-1", source="android", reason=None,
                              now=T0 + timedelta(seconds=12))                       # retransmis
        await rec.flush()
        await rec.stop()
        return a.session.session_id, sid, rec

    first, second, rec = asyncio.run(scenario())
    assert rec.failed == 0
    s1 = db.sessions.get(first)
    assert s1["state"] == "CANCELLED" and s1["end_reason"] == "ACCIDENTAL_ACTIVATION"
    assert db.alerts.list(session_id=first) == []

    s2 = db.sessions.get(second)
    assert s2["state"] == "ALERT"
    [alert] = db.alerts.list(session_id=second)
    assert alert["incident_type"] == "SOS_EXPLICIT" and alert["status"] == "ACTIVE"
    assert len(db.notifications.for_alert(alert["alert_id"])) == 1
    assert "SERVICE_UNAVAILABLE" in _types(db, second)


def test_db_outage_does_not_block_safety_logic(owner):
    class BrokenDb:
        def __getattr__(self, name):
            raise ConnectionError("Tiger Data unreachable")

    async def scenario():
        mgr, rec = _manager(BrokenDb())
        rec._retry_base = 0.01
        await rec.start()
        act = await mgr.activate(event_id="p", device_id=owner, now=T0)
        res = await mgr.request_sos(act.session.session_id, token=act.control_token, event_id="s",
                                    source="android", reason=None, now=T0 + timedelta(seconds=1))
        await rec.flush()
        await rec.stop()
        return res, rec

    res, rec = asyncio.run(scenario())
    assert res.session.state.value == "ALERT"   # SOS funcționează fără baza de date
    assert rec.failed == 2
