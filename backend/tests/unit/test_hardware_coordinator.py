from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime

import pytest

from app.errors import ConflictError
from app.hardware import (
    HardwareActivationCoordinator,
    HardwareActivationEvent,
    HardwareMonitorStopped,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def _event(number: int) -> HardwareActivationEvent:
    return HardwareActivationEvent(
        event_id=f"gpio-{number}",
        device_id="vesta-gpio17",
        occurred_at=NOW,
    )


class SequenceMonitor:
    def __init__(self, *items: HardwareActivationEvent | BaseException) -> None:
        self.items = deque(items)

    def start(self) -> None:
        pass

    async def get_event(self) -> HardwareActivationEvent:
        if not self.items:
            raise HardwareMonitorStopped("finished")
        item = self.items.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


class RecordingManager:
    def __init__(self, *failures: BaseException | None) -> None:
        self.calls: list[dict[str, object]] = []
        self.failures = deque(failures)

    async def activate(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.failures:
            failure = self.failures.popleft()
            if failure is not None:
                raise failure
        return object()


def test_coordinator_forwards_the_complete_hardware_event() -> None:
    async def scenario() -> None:
        manager = RecordingManager()
        coordinator = HardwareActivationCoordinator(
            monitor=SequenceMonitor(_event(1)),
            manager=manager,
            retry_delay_seconds=0.001,
        )

        await coordinator.run(asyncio.Event())

        assert manager.calls == [
            {
                "event_id": "gpio-1",
                "device_id": "vesta-gpio17",
                "now": NOW,
            }
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["ALREADY_ACTIVE", "SESSION_ALREADY_ACTIVE"])
def test_already_active_is_semantic_and_does_not_stop_consumer(code: str) -> None:
    async def scenario() -> None:
        manager = RecordingManager(ConflictError(code, "active"), None)
        coordinator = HardwareActivationCoordinator(
            monitor=SequenceMonitor(_event(1), _event(2)),
            manager=manager,
            retry_delay_seconds=0.001,
        )

        await coordinator.run(asyncio.Event())

        assert [call["event_id"] for call in manager.calls] == ["gpio-1", "gpio-2"]

    asyncio.run(scenario())


def test_unexpected_activation_failure_is_logged_and_stream_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        manager = RecordingManager(RuntimeError("temporary"), None)
        coordinator = HardwareActivationCoordinator(
            monitor=SequenceMonitor(_event(1), _event(2)),
            manager=manager,
            retry_delay_seconds=0.001,
        )

        await coordinator.run(asyncio.Event())

        assert [call["event_id"] for call in manager.calls] == ["gpio-1", "gpio-2"]

    asyncio.run(scenario())
    assert "hardware activation failed" in caplog.text


def test_unexpected_monitor_failure_is_logged_and_stream_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        manager = RecordingManager()
        coordinator = HardwareActivationCoordinator(
            monitor=SequenceMonitor(RuntimeError("temporary"), _event(2)),
            manager=manager,
            retry_delay_seconds=0.001,
        )

        await coordinator.run(asyncio.Event())

        assert [call["event_id"] for call in manager.calls] == ["gpio-2"]

    asyncio.run(scenario())
    assert "hardware event read failed" in caplog.text


def test_stop_event_interrupts_a_blocked_monitor_read() -> None:
    class BlockingMonitor(SequenceMonitor):
        async def get_event(self) -> HardwareActivationEvent:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def scenario() -> None:
        stop_event = asyncio.Event()
        coordinator = HardwareActivationCoordinator(
            monitor=BlockingMonitor(),
            manager=RecordingManager(),
            retry_delay_seconds=0.001,
        )
        task = asyncio.create_task(coordinator.run(stop_event))
        await asyncio.sleep(0)

        stop_event.set()
        await asyncio.wait_for(task, timeout=0.2)

    asyncio.run(scenario())


def test_stop_event_interrupts_error_backoff_without_busy_loop() -> None:
    class FailingMonitor(SequenceMonitor):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def get_event(self) -> HardwareActivationEvent:
            self.calls += 1
            raise RuntimeError("offline")

    async def scenario() -> None:
        stop_event = asyncio.Event()
        monitor = FailingMonitor()
        coordinator = HardwareActivationCoordinator(
            monitor=monitor,
            manager=RecordingManager(),
            retry_delay_seconds=10,
        )
        task = asyncio.create_task(coordinator.run(stop_event))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        stop_event.set()
        await asyncio.wait_for(task, timeout=0.2)
        assert monitor.calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("delay", [0, -0.1])
def test_retry_delay_must_be_positive(delay: float) -> None:
    with pytest.raises(ValueError, match="retry_delay_seconds"):
        HardwareActivationCoordinator(
            monitor=SequenceMonitor(),
            manager=RecordingManager(),
            retry_delay_seconds=delay,
        )
