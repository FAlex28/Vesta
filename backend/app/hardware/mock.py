"""Deterministic laptop adapter for tests and controlled demonstrations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .base import HardwareActivationEvent, HardwareMonitorStopped


class MockJoystickMonitor:
    """A hold detector driven explicitly by a simulated UTC clock.

    ``press`` and ``release`` model stable level changes. Contact bounce can be
    represented by short alternating calls; every release resets the continuous
    hold timer. ``advance`` never sleeps and emits at most once before release.
    """

    def __init__(
        self,
        *,
        device_id: str = "vesta-gpio17",
        hold_seconds: float = 5.0,
        debounce_seconds: float = 0.08,
        initially_pressed: bool = False,
    ) -> None:
        if hold_seconds <= 0:
            raise ValueError("hold_seconds must be positive")
        if debounce_seconds < 0:
            raise ValueError("debounce_seconds must not be negative")
        self.device_id = device_id
        self.hold_seconds = hold_seconds
        self.debounce_seconds = debounce_seconds
        self._events: asyncio.Queue[HardwareActivationEvent | None] = asyncio.Queue()
        self._initially_pressed = initially_pressed
        self._pressed = initially_pressed
        self._pressed_at: datetime | None = None
        self._armed = False
        self._fired_for_press = False
        self._running = False

    @property
    def pending_events(self) -> int:
        return self._events.qsize()

    @property
    def is_armed(self) -> bool:
        return self._armed

    def start(self) -> None:
        if self._running:
            return
        while True:
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._running = True
        self._pressed = self._initially_pressed
        self._pressed_at = None
        self._fired_for_press = False
        # A low input at startup is not trusted. It must be observed released
        # before a later press can begin an activation hold.
        self._armed = not self._pressed

    def press(self, *, now: datetime) -> None:
        self._require_running()
        instant = _as_utc(now)
        if self._pressed:
            return
        self._pressed = True
        self._fired_for_press = False
        self._pressed_at = instant if self._armed else None

    def release(self, *, now: datetime) -> None:
        self._require_running()
        _as_utc(now)
        if not self._pressed:
            return
        self._pressed = False
        self._pressed_at = None
        self._fired_for_press = False
        self._armed = True

    def advance(self, *, now: datetime) -> HardwareActivationEvent | None:
        self._require_running()
        instant = _as_utc(now)
        if (
            not self._armed
            or not self._pressed
            or self._pressed_at is None
            or self._fired_for_press
            or instant - self._pressed_at < timedelta(seconds=self.hold_seconds)
        ):
            return None

        event = HardwareActivationEvent(
            event_id=f"gpio-{uuid4()}",
            device_id=self.device_id,
            occurred_at=instant,
            source="mock_gpio",
        )
        self._fired_for_press = True
        self._armed = False
        self._events.put_nowait(event)
        return event

    async def get_event(self) -> HardwareActivationEvent:
        item = await self._events.get()
        if item is None:
            raise HardwareMonitorStopped("mock joystick monitor is stopped")
        return item

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._armed = False
        self._pressed_at = None
        self._events.put_nowait(None)

    def close(self) -> None:
        self.stop()

    def _require_running(self) -> None:
        if not self._running:
            raise HardwareMonitorStopped("mock joystick monitor is not running")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("hardware event times must be timezone-aware")
    return value.astimezone(UTC)
