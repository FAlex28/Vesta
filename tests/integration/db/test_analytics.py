"""Analize live din Tiger Data: agregate continue în timp real, timpi de răspuns, compresie."""

from __future__ import annotations

from datetime import UTC, datetime

from app.integrations.outbox import OutboxDispatcher


def test_live_analytics_snapshot(fresh_db, ev):
    db = fresh_db
    sid = db.sessions.activate("owner-analytics", ev()).session["session_id"]
    db.locations.record(session_id=sid, lat=44.43, lon=26.10, accuracy_m=12.0, source="DEVICE_GPS")
    db.locations.record(session_id=sid, lat=44.43, lon=26.10, accuracy_m=250.0, source="DEVICE_GPS")
    db.events.log_technical_event(component="POWER", event_type="LOW_VOLTAGE")
    db.events.log_technical_event(component="GPS", event_type="GPS_AVAILABLE", session_id=sid)

    r = db.alerts.raise_alert(sid, "SOS_EXPLICIT", event_id=ev(), recipients=["c"])
    OutboxDispatcher(db, lambda n: None, batch=50).run_once()
    db.notifications.mark_delivered(r.notifications_queued[0]["notification_id"])

    snap = db.analytics.snapshot(days=1, hours=1)

    live = snap["live"]
    assert live["open_sessions"] == 1 and live["active_alerts"] == 1
    assert live["last_gps_fix_at"] is not None

    # agregatele sunt în timp real: rândurile de acum apar fără refresh manual
    assert "SESSION_ACTIVATED" in {d["event_type"] for d in snap["daily"]}
    health = {(h["component"], h["event_type"]): h["events"] for h in snap["device_health"]}
    assert health[("POWER", "LOW_VOLTAGE")] == 1 and health[("GPS", "GPS_AVAILABLE")] == 1

    gps = [g for g in snap["gps_quality"] if g["source"] == "DEVICE_GPS"]
    assert sum(g["fixes"] for g in gps) == 2
    assert sum(g["imprecise_fixes"] for g in gps) == 1
    assert "lat" not in gps[0] and "lon" not in gps[0]  # agregatul nu expune coordonate

    resp = snap["alert_response"]
    assert resp["notifications"] == 1 and resp["delivered"] == 1
    assert resp["p50_delivered_s"] is not None and resp["p50_delivered_s"] >= 0

    storage = {s["name"]: s for s in snap["storage"]}
    assert set(storage) == {"session_events", "technical_events", "locations"}
    assert all(s["total_bytes"] > 0 for s in storage.values())


def test_compression_policies_present(fresh_db):
    with fresh_db.pool.autocommit() as tx:
        rows = tx.all(
            "SELECT hypertable_name, compression_enabled FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema = current_schema()"
        )
        jobs = tx.all(
            "SELECT hypertable_name, proc_name FROM timescaledb_information.jobs "
            "WHERE hypertable_schema = current_schema()"
        )
    assert all(r["compression_enabled"] for r in rows)
    procs = {(j["hypertable_name"], j["proc_name"]) for j in jobs}
    for table in ("session_events", "technical_events", "locations"):
        assert (table, "policy_compression") in procs


def test_compressed_chunk_still_queryable(fresh_db, ev):
    """Un chunk vechi comprimat manual rămâne citibil cu SQL simplu și chiar ocupă mai puțin."""
    db = fresh_db
    old = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    sid = db.sessions.activate("owner-compress", ev(), occurred_at=old).session["session_id"]
    for i in range(200):
        db.events.log_technical_event(
            component="PHONE", event_type="PHONE_HEARTBEAT_MISSED", session_id=sid,
            occurred_at=old.replace(minute=i % 60, second=i % 60),
        )
    with db.pool.autocommit() as tx:
        tx.all(
            "SELECT compress_chunk(c, if_not_compressed => true) "
            "FROM show_chunks('technical_events', older_than => now() - INTERVAL '30 days') c"
        )
        n = tx.one(
            "SELECT count(*) AS n FROM technical_events WHERE session_id = %s", (sid,)
        )["n"]
    assert n == 200
    stats = {s["name"]: s for s in db.analytics.storage()}["technical_events"]
    assert stats["compressed_chunks"] >= 1
