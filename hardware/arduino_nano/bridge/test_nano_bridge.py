"""Teste pentru logica bridge-ului (fără placă, fără backend).

Rulare: python -m pytest hardware/arduino_nano/bridge -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nano_bridge import (  # noqa: E402
    BACKEND_OFFLINE,
    NanoView,
    SimulatedNano,
    derive_target,
    full_sync_commands,
    plan_commands,
)

DEVICE = "vesta-rpi-01"


def session(state, *, sid="s1", alert=None, reason=None, issues=(), updated="2026-11-14T10:00:00Z",
            device=DEVICE):  # fmt: skip
    return {
        "id": sid,
        "device_id": device,
        "state": state,
        "updated_at": updated,
        "active_technical_issues": list(issues),
        "alert_reason": reason,
        "alert": alert,
    }


def status(*sessions, power=None):
    return {"sessions": list(sessions), "power": power}


def target_of(st, **kw):
    return derive_target(st, device_id=DEVICE, backend_ok=True, **kw)


def apply(nano: SimulatedNano, commands):
    # Prin send(), exact ca bridge-ul, ca să prindem și transformările de pe fir.
    replies = []
    nano.start(lambda kind, text: replies.append(text) if kind == "line" else None)
    replies.clear()
    for command in commands:
        nano.send(command)
    return replies


# -- derive_target ------------------------------------------------------------------


def test_no_sessions_is_standby():
    target, resolved = target_of(status())
    assert target == NanoView("STANDBY", None, None, None)
    assert resolved == set()


def test_states_map_to_nano():
    assert target_of(status(session("ACTIVE")))[0].safety == "ACTIVE"
    assert target_of(status(session("CHECK_IN")))[0].safety == "CHECK"
    assert target_of(status(session("UNCONFIRMED")))[0].safety == "CHECK"
    assert target_of(status(session("CANCELLED")))[0].safety == "STANDBY"


def test_active_alert_and_reason():
    st = status(session("ALERT", alert={"id": "a1", "status": "ACTIVE"}, reason="SOS_EXPLICIT"))
    target, _ = target_of(st)
    assert (target.alert_id, target.alert_reason) == ("a1", "SOS_EXPLICIT")


def test_resolved_alert_is_reported():
    st = status(session("CANCELLED", alert={"id": "a1", "status": "RESOLVED"}))
    target, resolved = target_of(st)
    assert target.alert_id is None
    assert resolved == {"a1"}


def test_newest_unfinished_session_wins_and_old_cancelled_ignored():
    st = status(
        session("CANCELLED", sid="old", updated="2026-11-14T09:00:00Z"),
        session("CHECK_IN", sid="new", updated="2026-11-14T10:00:00Z"),
    )
    assert target_of(st)[0].safety == "CHECK"


def test_other_device_is_ignored():
    st = status(session("ACTIVE", device="alt-dispozitiv"))
    assert target_of(st)[0].safety == "STANDBY"


def test_technical_priority_ignores_gps_and_page_hidden():
    st = status(session("ACTIVE", issues=["gps_stale", "page_hidden"]))
    assert target_of(st)[0].technical is None
    st = status(session("ACTIVE", issues=["voice_timeout", "network_disconnected"]))
    assert target_of(st)[0].technical == "NETWORK_DISCONNECTED"


def test_pi_power_problem_is_technical():
    st = status(power={"state": "LOW_VOLTAGE", "detail": "", "checked_at": "x"})
    assert target_of(st)[0].technical == "LOW_VOLTAGE"


def test_backend_offline_keeps_last_alert():
    last = NanoView("STANDBY", "a1", "SOS_EXPLICIT", None)
    target, _ = derive_target(None, device_id=DEVICE, backend_ok=False, last_target=last)
    assert target.alert_id == "a1"
    assert target.technical == BACKEND_OFFLINE


# -- plan_commands + simulator (verifică și că placa acceptă comenzile) -----------------


def test_full_session_lifecycle_on_simulated_board():
    nano = SimulatedNano(show_panel=False)

    commands, shown = full_sync_commands(target_of(status())[0])
    assert all(not r.startswith("ERR") for r in apply(nano, commands))
    assert nano.safety == "STANDBY"

    steps = [
        status(session("ACTIVE")),
        status(session("CHECK_IN")),
        status(session("ALERT", alert={"id": "a1", "status": "ACTIVE"}, reason="MISSED_CHECK_INS")),
        status(session("CANCELLED", alert={"id": "a1", "status": "RESOLVED"})),
        status(session("CANCELLED", alert={"id": "a1", "status": "RESOLVED"}),
               session("ACTIVE", sid="s2", updated="2026-11-14T11:00:00Z")),  # fmt: skip
    ]
    expected = [
        ["STATE|ACTIVE"],
        ["STATE|CHECK"],
        ["ALERT_RAISED|a1|MISSED_CHECK_INS"],
        ["ALERT_RESOLVED|a1"],
        ["STATE|ACTIVE"],
    ]
    for st, want in zip(steps, expected, strict=True):
        target, resolved = target_of(st)
        commands, shown = plan_commands(shown, target, resolved)
        assert commands == want
        assert all(not r.startswith("ERR") for r in apply(nano, commands))
    assert nano.safety == "ACTIVE" and not nano.alert_id


def test_unchanged_status_sends_nothing():
    target, resolved = target_of(status(session("ACTIVE")))
    shown = NanoView("ACTIVE")
    assert plan_commands(shown, target, resolved) == ([], shown)


def test_state_is_deferred_until_alert_resolved():
    shown = NanoView("STANDBY", "a1", "SOS_EXPLICIT")
    # Alerta încă activă, starea sesiunii s-a schimbat: nu se trimite STATE (placa l-ar refuza).
    st = status(session("ALERT", alert={"id": "a1", "status": "ACTIVE"}, reason="SOS_EXPLICIT"))
    assert plan_commands(shown, *target_of(st))[0] == []


def test_vanished_unresolved_alert_keeps_alarm():
    shown = NanoView("STANDBY", "a1", "SOS_EXPLICIT")
    commands, new = plan_commands(shown, *target_of(status()))
    assert commands == []
    assert new.alert_id == "a1"


def test_new_alert_replaces_old_one():
    shown = NanoView("STANDBY", "a1", "SOS_EXPLICIT")
    st = status(session("ALERT", alert={"id": "a2", "status": "ACTIVE"}, reason="SOS_EXPLICIT"))
    commands, new = plan_commands(shown, *target_of(st))
    assert commands == ["ALERT_RAISED|a2|SOS_EXPLICIT"]
    assert new.alert_id == "a2"


def test_technical_error_raise_and_clear():
    shown = NanoView("ACTIVE")
    st = status(session("ACTIVE", issues=["microphone_denied"]))
    commands, shown = plan_commands(shown, *target_of(st))
    assert commands == ["TECHNICAL_ERROR|MICROPHONE_DENIED"]
    commands, shown = plan_commands(shown, *target_of(status(session("ACTIVE"))))
    assert commands == ["TECHNICAL_RESOLVED"]


def test_full_sync_during_alert_with_technical_error():
    st = status(session("ALERT", alert={"id": "a1", "status": "ACTIVE"}, reason="SOS_EXPLICIT",
                        issues=["audio_disconnected"]))  # fmt: skip
    commands, shown = full_sync_commands(target_of(st)[0])
    assert commands == [
        "HELLO",
        "READY",
        "ALERT_RAISED|a1|SOS_EXPLICIT",
        "TECHNICAL_ERROR|AUDIO_DISCONNECTED",
    ]
    nano = SimulatedNano(show_panel=False)
    assert all(not r.startswith("ERR") for r in apply(nano, commands))
    assert nano.alert_id == "a1" and nano.technical


def test_ids_and_reasons_are_sanitized_for_the_board():
    long_id = "x" * 50
    st = status(session("ALERT", alert={"id": long_id, "status": "ACTIVE"}, reason="a|b\nc"))
    commands, shown = plan_commands(NanoView(), *target_of(st))
    assert commands == [f"ALERT_RAISED|{'x' * 39}|a b c"]
    st = status(session("CANCELLED", alert={"id": long_id, "status": "RESOLVED"}))
    commands, _ = plan_commands(shown, *target_of(st))
    assert commands == [f"ALERT_RESOLVED|{'x' * 39}"]


# -- legătura cu PC-ul: repaus fără eroare, BYE, toleranța la backend repornit -----------


def test_idle_board_waits_for_pc_without_error():
    nano = SimulatedNano(show_panel=False)
    assert "Astept PC" in nano.panel() and "ALBASTRU" not in nano.panel()
    apply(nano, ["PING"])
    assert "Fara sesiune" in nano.panel()


def test_bye_returns_to_waiting_and_clears_technical():
    nano = SimulatedNano(show_panel=False)
    replies = apply(nano, ["HELLO", "STATE|ACTIVE", "TECHNICAL_ERROR|SERVER OFFLINE", "BYE"])
    assert replies[-1] == "OK|BYE"
    assert nano.safety == "STANDBY" and not nano.technical
    assert "Astept PC" in nano.panel()


def test_bye_never_stops_an_active_alert():
    nano = SimulatedNano(show_panel=False)
    apply(nano, ["ALERT_RAISED|a1|SOS_EXPLICIT", "BYE"])
    assert nano.alert_id == "a1" and "ROSU" in nano.panel()


def test_backend_grace_period():
    import nano_bridge

    bridge = nano_bridge.Bridge(SimulatedNano(show_panel=False), None, device_id=DEVICE,
                                verbose=False)  # fmt: skip
    bridge._backend_down()
    assert bridge.backend_in_grace()
    bridge.backend_down_since -= nano_bridge.BACKEND_GRACE_S + 1
    assert not bridge.backend_in_grace()
