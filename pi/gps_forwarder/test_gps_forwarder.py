"""NMEA parsing for the device GPS. Run: python -m pytest pi/gps_forwarder -q"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gps_forwarder import GpsState, checksum_ok, parse_sentence  # noqa: E402


def nmea(body: str) -> str:
    value = 0
    for char in body:
        value ^= ord(char)
    return f"${body}*{value:02X}"


def test_checksum() -> None:
    sentence = nmea("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,")
    assert checksum_ok(sentence)
    assert not checksum_ok(sentence[:-2] + "00")
    assert not checksum_ok("garbage")


def test_rmc_fix_and_gga_quality() -> None:
    state = GpsState()
    assert not parse_sentence(nmea("GNGGA,101530.00,4544.9346,N,02112.5232,E,1,07,1.20,90.1,M,37.0,M,,"), state)
    assert state.satellites == 7 and state.hdop == 1.2 and state.accuracy_meters == 6.0
    assert parse_sentence(nmea("GNRMC,101530.00,A,4544.9346,N,02112.5232,E,0.10,,151126,,,A"), state)
    assert round(state.latitude, 5) == 45.74891 and round(state.longitude, 5) == 21.20872
    assert state.fix_time == datetime(2026, 11, 15, 10, 15, 30, tzinfo=UTC)


def test_no_fix_and_southern_western_hemisphere() -> None:
    state = GpsState()
    assert not parse_sentence(nmea("GPRMC,101530.00,V,,,,,,,151126,,,N"), state)
    assert not state.valid
    assert parse_sentence(nmea("GPRMC,000001,A,3352.000,S,15112.000,W,0,0,010126,,"), state)
    assert state.latitude < 0 and state.longitude < 0


def test_corrupted_sentence_is_ignored() -> None:
    state = GpsState()
    assert not parse_sentence("$GNRMC,101530.00,A,4544.93*FF", state)
    assert state.sentences == 0
