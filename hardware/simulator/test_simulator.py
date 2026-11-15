"""Teste pentru hardware-ul virtual: python -m pytest hardware/simulator -q"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(Path(__file__).parent), str(ROOT / "hardware" / "arduino_nano" / "bridge"),
                str(ROOT / "pi" / "gps_forwarder")]  # fmt: skip

import gps_forwarder  # noqa: E402
import nano_bridge  # noqa: E402
from virtual_nano import NanoFirmware, VirtualNano  # noqa: E402
from vesta_sim import VirtualGps  # noqa: E402

SEQUENCE = [
    "HELLO", "PING", "STATUS", "STATE|ACTIVE", "STATE|CHECK_IN", "STATE|BOGUS", "STATE",
    "ALERT_RAISED|abc123|SOS_EXPLICIT", "STATE|ACTIVE", "STATUS", "ALERT_RESOLVED|other",
    "ALERT_RESOLVED|abc123", "TECHNICAL_ERROR|MICROPHONE_DENIED", "STATUS", "TECHNICAL_RESOLVED",
    "DELIVERY_FAILED|n1", "DELIVERED|n1", "ALERT_RAISED", "ALERT_RAISED|x|CHECKIN_TIMEOUT", "CLEAR",
    "ALERT_RESOLVED|x", "CLEAR", "READY", "NOPE", "BYE", "STATUS",
]  # fmt: skip


def test_replies_match_the_bridge_reference_simulator() -> None:
    """Două oglindiri independente ale sketch-ului dau aceleași răspunsuri (fără PONG/millis)."""
    reference = nano_bridge.SimulatedNano(show_panel=False)
    firmware = NanoFirmware()
    for command in SEQUENCE:
        expected = reference.process(command)
        got = firmware.process_command(command, now=1000)
        if command == "PING":
            assert got[0].startswith("PONG|") and expected.startswith("PONG|")
            continue
        if command == "STATUS" and "|ALERT=0|" in got[0]:
            # Diferență cunoscută: sketch-ul păstrează în activeAlertId ID-ul ultimei alerte
            # rezolvate (STATUS îl arată), SimulatedNano îl golește. Placa virtuală urmează sketch-ul.
            got, expected = [got[0].split("|ID=")[0]], expected.split("|ID=")[0]
        assert got == ([expected] if expected else []), command


def test_link_lost_after_10_s_without_commands_only_alarms_with_a_session() -> None:
    fw = NanoFirmware()
    fw.process_command("STATE|ACTIVE", now=0)
    assert fw.update_link(10_000) == []
    assert fw.update_link(10_001) == ["LINK|LOST|SESSION"]
    assert fw.led()[0] == "ALBASTRU" and fw.lcd(10_001) == ("ALBASTRU 04 ERR", "CONEXIUNE PC")

    idle = NanoFirmware()
    idle.process_command("TECHNICAL_ERROR|LOW_VOLTAGE", now=0)
    assert idle.update_link(10_001) == ["LINK|LOST|IDLE"]
    assert idle.lcd(10_001) == ("ALB 00 STANDBY", "Astept PC") and idle.sound(10_001) == "liniste"


def test_alert_keeps_ringing_without_pc_and_resolved_banner_lasts_5_s() -> None:
    fw = NanoFirmware()
    fw.process_command("ALERT_RAISED|a1|SOS_EXPLICIT", now=0)
    fw.update_link(20_000)
    assert fw.led()[0] == "ROSU" and fw.sound(20_000).startswith("D7")
    fw.process_command("ALERT_RESOLVED|a1", now=21_000)
    assert fw.lcd(25_999)[1] == "Alerta rezolvata"
    assert fw.lcd(26_000)[1] == "Fara sesiune"
    assert fw.sound(21_100).startswith("D8 ton scurt")


def test_serial_buffer_overflow_and_strtok_semantics() -> None:
    fw = NanoFirmware()
    assert fw.feed(b"A" * 130 + b"\n", now=0) == ["ERR|LINE_TOO_LONG"]
    assert fw.feed(b"ALERT_RAISED||SOS_EXPLICIT\r\n", now=0) == ["OK|ALERT_RAISED|SOS_EXPLICIT"]
    long_id = "x" * 60
    assert fw.process_command(f"ALERT_RESOLVED|{long_id}", now=0) == ["ERR|ALERT_ID_MISMATCH"]


def test_real_serial_link_talks_to_the_virtual_board_over_socket() -> None:
    pytest.importorskip("serial")
    nano = VirtualNano(port=7799)
    nano.start()
    lines: list[str] = []
    got_boot = threading.Event()

    def callback(kind: str, text: str) -> None:
        if kind == "line":
            lines.append(text)
            if text.startswith("BOOT|"):
                got_boot.set()

    link = nano_bridge.SerialLink("socket://127.0.0.1:7799")
    link.start(callback)
    try:
        assert got_boot.wait(8), lines
        assert link.send("HELLO") and link.send("STATE|ACTIVE")
        deadline = time.monotonic() + 3
        while "OK|STATE|ACTIVE" not in lines and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "HELLO|VESTA_NANO|1" in lines and "OK|STATE|ACTIVE" in lines
        assert nano.view()["led"] == "VERDE"
    finally:
        link.close()
        nano.stop()


def test_virtual_gps_sentences_pass_the_real_nmea_parser() -> None:
    gps, state = VirtualGps(), gps_forwarder.GpsState()
    fixes = [gps_forwarder.parse_sentence(s, state) for s in gps.sentences()]
    assert fixes == [False, True] and state.valid and state.satellites == 8
    assert abs(state.latitude - gps.position[0]) < 1e-5 and abs(state.longitude - gps.position[1]) < 1e-5
    assert state.accuracy_meters == 6.0  # HDOP 1.2 x 5 m

    gps.fix = False
    assert [gps_forwarder.parse_sentence(s, state) for s in gps.sentences()] == [False, False]
    assert not state.valid
