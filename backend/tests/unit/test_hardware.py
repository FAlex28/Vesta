from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.hardware import (
    GpioZeroJoystickMonitor,
    HardwareMonitorAlreadyRunning,
    HardwareMonitorStopped,
    MockJoystickMonitor,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def test_mock_short_press_does_not_activate() -> None:
    monitor = MockJoystickMonitor(hold_seconds=5.0)
    monitor.start()

    monitor.press(now=NOW)
    assert monitor.advance(now=NOW + timedelta(seconds=4.999)) is None
    monitor.release(now=NOW + timedelta(seconds=4.999))
    assert monitor.pending_events == 0


def test_mock_exact_hold_activates_without_release() -> None:
    monitor = MockJoystickMonitor(hold_seconds=5.0)
    monitor.start()
    monitor.press(now=NOW)

    event = monitor.advance(now=NOW + timedelta(seconds=5))

    assert event is not None
    assert event.device_id == "vesta-gpio17"
    assert event.source == "mock_gpio"
    assert monitor.pending_events == 1


def test_mock_ten_second_hold_emits_only_once_until_release() -> None:
    async def scenario() -> None:
        monitor = MockJoystickMonitor(hold_seconds=5.0)
        monitor.start()
        monitor.press(now=NOW)

        first = monitor.advance(now=NOW + timedelta(seconds=5))
        assert monitor.advance(now=NOW + timedelta(seconds=10)) is None
        assert monitor.pending_events == 1
        queued = await monitor.get_event()
        assert queued == first

        monitor.release(now=NOW + timedelta(seconds=11))
        monitor.press(now=NOW + timedelta(seconds=12))
        second = monitor.advance(now=NOW + timedelta(seconds=17))
        assert second is not None
        assert first is not None
        assert second.event_id != first.event_id

    asyncio.run(scenario())


def test_mock_startup_pressed_requires_release_before_arming() -> None:
    monitor = MockJoystickMonitor(initially_pressed=True, hold_seconds=5.0)
    monitor.start()

    assert monitor.is_armed is False
    assert monitor.advance(now=NOW + timedelta(seconds=20)) is None
    monitor.release(now=NOW + timedelta(seconds=21))
    assert monitor.is_armed is True
    monitor.press(now=NOW + timedelta(seconds=22))
    assert monitor.advance(now=NOW + timedelta(seconds=27)) is not None


def test_mock_contact_bounce_never_counts_as_continuous_hold() -> None:
    monitor = MockJoystickMonitor(hold_seconds=5.0, debounce_seconds=0.08)
    monitor.start()

    monitor.press(now=NOW)
    monitor.release(now=NOW + timedelta(milliseconds=20))
    monitor.press(now=NOW + timedelta(milliseconds=40))
    monitor.release(now=NOW + timedelta(milliseconds=60))
    monitor.press(now=NOW + timedelta(milliseconds=100))

    assert monitor.advance(now=NOW + timedelta(seconds=5)) is None
    assert monitor.advance(now=NOW + timedelta(seconds=5, milliseconds=100)) is not None
    assert monitor.pending_events == 1


def test_mock_stop_closes_event_stream() -> None:
    async def scenario() -> None:
        monitor = MockJoystickMonitor()
        monitor.start()
        monitor.stop()
        with pytest.raises(HardwareMonitorStopped):
            await monitor.get_event()

    asyncio.run(scenario())


class FakeLock:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.acquired = 0
        self.released = 0

    def acquire(self) -> None:
        if self.fail:
            raise HardwareMonitorAlreadyRunning("owned")
        self.acquired += 1

    def release(self) -> None:
        self.released += 1


class FakeButton:
    def __init__(self, *, initially_pressed: bool = False) -> None:
        self.is_pressed = initially_pressed
        self.when_held = None
        self.when_released = None
        self.closed = False

    def trigger_held(self) -> None:
        if self.when_held is not None:
            self.when_held()

    def trigger_released(self) -> None:
        self.is_pressed = False
        if self.when_released is not None:
            self.when_released()

    def close(self) -> None:
        self.closed = True


def test_gpiozero_adapter_uses_final_pin_and_hold_configuration() -> None:
    async def scenario() -> None:
        captured: dict[str, object] = {}
        button = FakeButton()
        lock = FakeLock()

        def factory(pin: int, **kwargs: object) -> FakeButton:
            captured["pin"] = pin
            captured.update(kwargs)
            return button

        monitor = GpioZeroJoystickMonitor(button_factory=factory, instance_lock=lock)
        monitor.start()

        assert captured == {
            "pin": 17,
            "pull_up": True,
            "bounce_time": 0.08,
            "hold_time": 5.0,
            "hold_repeat": False,
        }
        assert not hasattr(button, "when_pressed")
        button.trigger_held()
        first = await asyncio.wait_for(monitor.get_event(), timeout=0.2)
        button.trigger_held()
        await asyncio.sleep(0)
        assert monitor._queue.qsize() == 0

        button.trigger_released()
        button.trigger_held()
        second = await asyncio.wait_for(monitor.get_event(), timeout=0.2)
        assert first.event_id != second.event_id

        monitor.stop()
        assert button.closed is True
        assert lock.acquired == 1
        assert lock.released == 1

    asyncio.run(scenario())


def test_gpiozero_startup_pressed_interlock_and_single_event() -> None:
    async def scenario() -> None:
        button = FakeButton(initially_pressed=True)
        monitor = GpioZeroJoystickMonitor(
            button_factory=lambda *_args, **_kwargs: button,
            instance_lock=FakeLock(),
        )
        monitor.start()

        assert monitor.is_armed is False
        button.trigger_held()
        await asyncio.sleep(0)
        assert monitor._queue.qsize() == 0

        button.trigger_released()
        assert monitor.is_armed is True
        button.trigger_held()
        await asyncio.wait_for(monitor.get_event(), timeout=0.2)
        button.trigger_held()
        await asyncio.sleep(0)
        assert monitor._queue.qsize() == 0
        monitor.stop()

    asyncio.run(scenario())


def test_gpiozero_singleton_failure_does_not_construct_button() -> None:
    async def scenario() -> None:
        constructed = False

        def factory(*_args: object, **_kwargs: object) -> FakeButton:
            nonlocal constructed
            constructed = True
            return FakeButton()

        monitor = GpioZeroJoystickMonitor(
            button_factory=factory,
            instance_lock=FakeLock(fail=True),
        )
        with pytest.raises(HardwareMonitorAlreadyRunning):
            monitor.start()
        assert constructed is False

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"pin": -1}, "pin"),
        ({"hold_seconds": 0}, "hold_seconds"),
        ({"debounce_seconds": -1}, "debounce_seconds"),
        ({"queue_size": 0}, "queue_size"),
    ],
)
def test_gpiozero_configuration_validation(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        GpioZeroJoystickMonitor(**kwargs)
