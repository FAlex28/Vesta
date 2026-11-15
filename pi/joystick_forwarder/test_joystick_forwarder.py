"""Teste pentru livrarea activărilor (fără GPIO, fără rețea).

Rulare: python -m pytest pi/joystick_forwarder -q
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from joystick_forwarder import (  # noqa: E402
    Activation,
    PermanentError,
    TemporaryError,
    deliver,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def activation() -> Activation:
    item = Activation.new()
    return Activation(item.event_id, item.occurred_at, 0.0)


def test_retries_with_the_same_event_id_until_delivered():
    seen: list[str] = []

    def send(a: Activation) -> str:
        seen.append(a.event_id)
        if len(seen) < 3:
            raise TemporaryError("connection refused")
        return "ACTIVATED"

    result = deliver(activation(), send, stop=threading.Event(), now=FakeClock(), delays=(0, 0))
    assert result == "ACTIVATED"
    assert len(seen) == 3 and len(set(seen)) == 1


def test_wrong_key_is_not_retried():
    calls = []

    def send(a: Activation) -> str:
        calls.append(a)
        raise PermanentError("HTTP 401")

    assert deliver(activation(), send, stop=threading.Event(), now=FakeClock(), delays=(0,)) is None
    assert len(calls) == 1


def test_old_activation_expires_instead_of_starting_a_late_session():
    clock = FakeClock()

    def send(a: Activation) -> str:
        clock.t += 50
        raise TemporaryError("timeout")

    result = deliver(activation(), send, stop=threading.Event(), now=clock, max_age=120,
                     delays=(0,))  # fmt: skip
    assert result is None and clock.t >= 120


def test_stop_ends_delivery():
    stop = threading.Event()
    stop.set()
    assert deliver(activation(), lambda a: "ACTIVATED", stop=stop) is None
