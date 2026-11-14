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
    policy = SafetyPolicy(
        check_in_interval=timedelta(seconds=60),
        response_timeout=timedelta(seconds=15),
        retry_delay=timedelta(seconds=5),
        max_attempts=2,
    )
    recorder = TigerRecorder(db, recipients=("contact-1",), response_timeout_s=15)
    mgr = SessionManager(
        engine=SafetyEngine(policy),
        broker=EventBroker(),
        token_service=ControlTokenService("x" * 48, default_ttl=timedelta(hours=1)),
        control_token_ttl_seconds=3600,
        scheduler_tick_seconds=0.1,
        client_heartbeat_timeout_seconds=20,
        recorder=recorder,
    )
    return mgr, recorder


def _types(db, sid):
    return [e["event_type"] for e in reversed(db.events.timeline(sid))]


def test_full_flow_checkin_missed_alert_then_explicit_resolution(db, owner):
    async def scenario():
        mgr, rec = _manager(db)
        await rec.start()
        act = await mgr.activate(event_id="press-1", device_id=owner, now=T0)
        sid, tok = act.session.session_id, act.control_token
        await mgr.activate(event_id="press-1", device_id=owner, now=T0)  # retransmis

        await mgr.advance_all(now=T0 + timedelta(seconds=61))  # CHECK_IN #1
        first = await mgr.get(sid)
        assert first.current_checkin_id is not None
        await mgr.mark_checkin_delivery(
            sid,
            first.current_checkin_id,
            token=tok,
            event_id="delivery-1",
            delivered=True,
            channel="voice",
            now=T0 + timedelta(seconds=62),
        )
        await mgr.confirm_safe(
            sid,
            token=tok,
            event_id="ok-1",
            checkin_id=first.current_checkin_id,
            source="android",
            now=T0 + timedelta(seconds=65),
        )
        await mgr.advance_all(now=T0 + timedelta(seconds=126))  # CHECK_IN #1 (runda 2)
        second = await mgr.get(sid)
        assert second.current_checkin_id is not None
        await mgr.mark_checkin_delivery(
            sid,
            second.current_checkin_id,
            token=tok,
            event_id="delivery-2",
            delivered=True,
            channel="voice",
            now=T0 + timedelta(seconds=127),
        )
        await mgr.advance_all(now=T0 + timedelta(seconds=142))  # ratat → UNCONFIRMED
        await mgr.advance_all(now=T0 + timedelta(seconds=148))  # CHECK_IN #2
        third = await mgr.get(sid)
        assert third.current_checkin_id is not None
        await mgr.mark_checkin_delivery(
            sid,
            third.current_checkin_id,
            token=tok,
            event_id="delivery-3",
            delivered=True,
            channel="voice",
            now=T0 + timedelta(seconds=149),
        )
        await mgr.advance_all(now=T0 + timedelta(seconds=164))  # ratat → ALERT
        alert = mgr.alerts.get_for_session(sid)
        assert alert is not None
        await mgr.resolve_alert(
            sid,
            alert.alert_id,
            token=tok,
            event_id="resolve-1",
            action="USER_CONFIRMED_SAFE",
            now=T0 + timedelta(seconds=200),
        )
        await rec.flush()
        await rec.stop()
        return (
            sid,
            [
                first.current_checkin_id,
                second.current_checkin_id,
                third.current_checkin_id,
            ],
            alert.alert_id,
            rec,
        )

    sid, engine_checkin_ids, engine_alert_id, rec = asyncio.run(scenario())
    assert rec.failed == 0 and rec.dropped == 0

    s = db.sessions.get(sid)
    assert (
        s["owner_id"] == owner
        and s["state"] == "CANCELLED"
        and s["end_reason"] == "ALERT_RESOLVED"
    )
    assert len(db.sessions.list_recent(owner)) == 1

    checkins = db.checkins.for_session(sid)
    assert [str(c["checkin_id"]) for c in checkins] == engine_checkin_ids
    assert [(c["round_no"], c["attempt_no"], c["status"]) for c in checkins] == [
        (1, 1, "CONFIRMED"),
        (2, 1, "TIMED_OUT"),
        (2, 2, "TIMED_OUT"),
    ]
    assert all(c["delivery_channel"] == "AUDIO" for c in checkins)
    assert all(c["delivered_at"] is not None for c in checkins)

    [alert] = db.alerts.list(session_id=sid)
    assert str(alert["alert_id"]) == engine_alert_id
    assert alert["incident_type"] == "CHECKIN_TIMEOUT" and alert["status"] == "RESOLVED"
    assert alert["resolution_reason"] == "USER_CONFIRMED_SAFE"
    notifications = db.notifications.for_alert(alert["alert_id"])
    assert {str(n["alert_id"]) for n in notifications} == {engine_alert_id}
    assert all(n["payload"]["alert_id"] == engine_alert_id for n in notifications)
    kinds = sorted(n["kind"] for n in notifications)
    assert kinds == ["ALERT_RAISED", "ALERT_RESOLVED"]

    types = _types(db, sid)
    assert types[0] == "SESSION_ACTIVATED" and types.count("SESSION_ACTIVATED") == 1
    assert types.count("CHECKIN_TIMED_OUT") == 2
    assert "ALERT_RAISED" in types and types[-1] == "ALERT_RESOLVED"


def test_undelivered_checkin_is_persisted_without_counting_as_missed(db, owner):
    async def scenario():
        mgr, rec = _manager(db)
        await rec.start()
        act = await mgr.activate(event_id="press-undelivered", device_id=owner, now=T0)
        sid, tok = act.session.session_id, act.control_token

        await mgr.advance_all(now=T0 + timedelta(seconds=61))
        requested = await mgr.get(sid)
        assert requested.current_checkin_id is not None
        await mgr.mark_checkin_delivery(
            sid,
            requested.current_checkin_id,
            token=tok,
            event_id="delivery-undelivered",
            delivered=False,
            channel="voice",
            now=T0 + timedelta(seconds=62),
        )
        await mgr.advance_all(now=T0 + timedelta(seconds=76))
        result = await mgr.get(sid)
        await rec.flush()
        await rec.stop()
        return sid, requested.current_checkin_id, result, rec

    sid, engine_checkin_id, result, rec = asyncio.run(scenario())
    assert rec.failed == 0 and rec.dropped == 0
    assert result.state.value == "UNCONFIRMED"
    assert result.missed_attempts == 0
    assert result.undelivered_attempts == 1

    [checkin] = db.checkins.for_session(sid)
    assert str(checkin["checkin_id"]) == engine_checkin_id
    assert checkin["status"] == "TIMED_OUT"
    assert checkin["delivery_channel"] is None
    assert checkin["delivered_at"] is None
    assert db.sessions.get(sid)["state"] == "UNCONFIRMED"

    types = _types(db, sid)
    assert "CHECKIN_AUDIO_UNAVAILABLE" in types
    assert "CHECKIN_TIMED_OUT" in types
    assert "ALERT_RAISED" not in types


def test_sos_quick_cancel_and_technical_issues(db, owner):
    async def scenario():
        mgr, rec = _manager(db)
        await rec.start()
        a = await mgr.activate(event_id="p-a", device_id=owner, now=T0)
        await mgr.cancel(
            a.session.session_id,
            token=a.control_token,
            event_id="c-a",
            reason="ACCIDENTAL_ACTIVATION",
            now=T0 + timedelta(seconds=2),
        )

        b = await mgr.activate(
            event_id="p-b", device_id=owner, now=T0 + timedelta(seconds=10)
        )
        sid, tok = b.session.session_id, b.control_token
        from app.safety import TechnicalIssue

        await mgr.set_technical_issue(
            sid,
            issue=TechnicalIssue.VOICE_UNAVAILABLE,
            active=True,
            event_id="v-1",
            source="voice_gateway",
            token=tok,
            now=T0 + timedelta(seconds=11),
        )
        await mgr.request_sos(
            sid,
            token=tok,
            event_id="sos-1",
            source="android",
            reason=None,
            now=T0 + timedelta(seconds=12),
        )
        await mgr.request_sos(
            sid,
            token=tok,
            event_id="sos-1",
            source="android",
            reason=None,
            now=T0 + timedelta(seconds=12),
        )  # retransmis
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
        res = await mgr.request_sos(
            act.session.session_id,
            token=act.control_token,
            event_id="s",
            source="android",
            reason=None,
            now=T0 + timedelta(seconds=1),
        )
        await rec.flush()
        await rec.stop()
        return res, rec

    res, rec = asyncio.run(scenario())
    assert res.session.state.value == "ALERT"  # SOS funcționează fără baza de date
    assert rec.failed == 2
