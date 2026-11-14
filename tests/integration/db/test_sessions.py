import threading
import uuid

import pytest

from app.db.errors import (
    AlertStillActive,
    InvalidTransition,
    SessionClosed,
    StaleVersion,
)


def _events(db, session_id, event_type=None):
    rows = db.events.timeline(session_id)
    return [r for r in rows if event_type is None or r["event_type"] == event_type]


# UC1 — o apăsare ignorată (sub 5 s) nu ajunge deloc în DB: backendul nu apelează activate().
def test_no_session_without_activation(db, owner):
    assert db.sessions.get_open(owner) is None
    assert db.sessions.list_recent(owner) == []


# UC2 — același event_id retransmis → o singură sesiune, un singur SESSION_ACTIVATED
def test_duplicate_activation_event_is_idempotent(db, owner, ev):
    e = ev()
    r1 = db.sessions.activate(owner, e)
    r2 = db.sessions.activate(owner, e)
    r3 = db.sessions.activate(owner, str(e))
    assert r1.status == "CREATED"
    assert r2.status == r3.status == "DUPLICATE_EVENT"
    assert r2.session["session_id"] == r1.session["session_id"]
    assert len(db.sessions.list_recent(owner)) == 1
    assert len(_events(db, r1.session["session_id"], "SESSION_ACTIVATED")) == 1


# UC2 — bounce: multe activări concurente pentru același owner → o singură sesiune
def test_concurrent_activations_create_one_session(db, owner):
    results, barrier = [], threading.Barrier(5)

    def go():
        barrier.wait()
        results.append(db.sessions.activate(owner, uuid.uuid4()).status)

    threads = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results).count("CREATED") == 1
    assert results.count("ALREADY_ACTIVE") == 4
    assert len(db.sessions.list_recent(owner)) == 1


# UC3 — reapăsare în sesiune deschisă → ALREADY_ACTIVE, aceeași sesiune
def test_repress_in_active_session(db, owner, ev, active_session):
    r = db.sessions.activate(owner, ev())
    assert r.status == "ALREADY_ACTIVE"
    assert r.session["session_id"] == active_session["session_id"]
    assert len(_events(db, active_session["session_id"], "REACTIVATION_IGNORED")) == 1


# UC3 — după încheiere, istoricul nu blochează o sesiune nouă
def test_new_session_allowed_after_previous_closed(db, owner, ev, active_session):
    db.sessions.cancel(active_session["session_id"], event_id=ev())
    r = db.sessions.activate(owner, ev())
    assert r.status == "CREATED"
    assert len(db.sessions.list_recent(owner)) == 2


# UC4 — anulare rapidă în fereastra de 5 s, cu motiv ACCIDENTAL_ACTIVATION; istoricul rămâne
def test_quick_cancel_keeps_history(db, active_session, ev):
    sid = active_session["session_id"]
    r = db.sessions.cancel(sid, event_id=ev(), reason="ACCIDENTAL_ACTIVATION")
    assert r.quick_cancel is True
    assert r.session["state"] == "CANCELLED"
    assert r.session["end_reason"] == "ACCIDENTAL_ACTIVATION"
    assert r.session["ended_at"] is not None
    types = [e["event_type"] for e in _events(db, sid)]
    assert "SESSION_ACTIVATED" in types and "STATE_CANCELLED" in types
    assert db.notifications.for_alert(uuid.uuid4()) == []  # nicio notificare


def test_cancel_after_window_is_normal(db, owner, ev):
    s = db.sessions.activate(owner, ev(), cancel_window_s=0).session
    r = db.sessions.cancel(s["session_id"], event_id=ev())
    assert r.quick_cancel is False
    assert r.session["end_reason"] == "USER_ENDED"


def test_cancel_is_idempotent_and_closed_session_stays_closed(db, active_session, ev):
    sid, e = active_session["session_id"], ev()
    db.sessions.cancel(sid, event_id=e)
    assert db.sessions.cancel(sid, event_id=e).duplicate is True
    with pytest.raises(SessionClosed):
        db.sessions.cancel(sid, event_id=ev())
    with pytest.raises(SessionClosed):
        db.sessions.transition(sid, "ACTIVE", event_id=ev())


# UC4 — SOS în fereastra de anulare e procesat imediat
def test_sos_inside_cancel_window(db, active_session, ev):
    r = db.alerts.raise_alert(
        active_session["session_id"],
        "SOS_EXPLICIT",
        event_id=ev(),
        source="COMPANION",
        recipients=["contact-1"],
    )
    assert r.status == "CREATED"
    assert db.sessions.get(active_session["session_id"])["state"] == "ALERT"


# UC15 — anularea nu poate șterge / ocoli o alertă activă
def test_cancel_refused_while_alert_active(db, active_session, ev):
    db.alerts.raise_alert(active_session["session_id"], "SOS_EXPLICIT", event_id=ev())
    with pytest.raises(AlertStillActive):
        db.sessions.cancel(active_session["session_id"], event_id=ev())


def test_transition_version_and_validation(db, active_session, ev):
    sid = active_session["session_id"]
    s = db.sessions.transition(sid, "CHECK_IN", event_id=ev(), expected_version=1)
    assert s["state_version"] == 2
    with pytest.raises(StaleVersion):
        db.sessions.transition(sid, "UNCONFIRMED", event_id=ev(), expected_version=1)
    with pytest.raises(InvalidTransition):
        db.sessions.transition(sid, "CHECK_IN", event_id=ev())  # CHECK_IN → CHECK_IN
    e = ev()
    a = db.sessions.transition(sid, "UNCONFIRMED", event_id=e)
    b = db.sessions.transition(sid, "UNCONFIRMED", event_id=e)  # retransmis
    assert a["state_version"] == b["state_version"] == 3
