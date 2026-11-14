"""gpiozero adapter for the Raspberry Pi joystick input on BCM GPIO17."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .base import (
    HardwareActivationEvent,
    HardwareMonitorAlreadyRunning,
    HardwareMonitorStopped,
    HardwareMonitorUnavailable,
)


class ButtonLike(Protocol):
    is_pressed: bool
    when_held: Callable[[], None] | None
    when_released: Callable[[], None] | None

    def close(self) -> None: ...


ButtonFactory = Callable[..., ButtonLike]


class ExclusiveLock(Protocol):
    def acquire(self) -> None: ...

    def release(self) -> None: ...


class ProcessFileLock:
    """A non-blocking inter-process lock kept for the monitor lifetime."""

    def __init__(self, path: str | Path = "/tmp/vesta-gpio17.lock") -> None:
        self.path = Path(path)
        self._file: Any | None = None

    def acquire(self) -> None:
        if self._file is not None:
            raise HardwareMonitorAlreadyRunning("GPIO lock is already held by this monitor")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif os.name == "nt":
                import msvcrt

                if lock_file.tell() == 0:
                    lock_file.write(b"0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - unsupported operating system
                raise HardwareMonitorUnavailable(f"No process-lock implementation for {os.name!r}")
        except (BlockingIOError, OSError) as exc:
            lock_file.close()
            raise HardwareMonitorAlreadyRunning(
                "another process already owns the GPIO17 monitor"
            ) from exc
        self._file = lock_file

    def release(self) -> None:
        lock_file = self._file
        if lock_file is None:
            return
        self._file = None
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lock_file.close()


class GpioZeroJoystickMonitor:
    """Convert one continuous gpiozero hold into one internal activation event."""

    def __init__(
        self,
        *,
        pin: int = 17,
        device_id: str = "vesta-gpio17",
        hold_seconds: float = 5.0,
        debounce_seconds: float = 0.08,
        queue_size: int = 8,
        button_factory: ButtonFactory | None = None,
        instance_lock: ExclusiveLock | None = None,
    ) -> None:
        if pin < 0:
            raise ValueError("pin must not be negative")
        if hold_seconds <= 0:
            raise ValueError("hold_seconds must be positive")
        if debounce_seconds < 0:
            raise ValueError("debounce_seconds must not be negative")
        if queue_size < 1:
            raise ValueError("queue_size must be at least one")
        self.pin = pin
        self.device_id = device_id
        self.hold_seconds = hold_seconds
        self.debounce_seconds = debounce_seconds
        self._queue: asyncio.Queue[HardwareActivationEvent | None] = asyncio.Queue(
            maxsize=queue_size
        )
        self._button_factory = button_factory or _gpiozero_button_factory
        self._instance_lock = instance_lock or ProcessFileLock()
        self._button: ButtonLike | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._armed = False
        self._fired_for_press = False
        self._state_lock = threading.Lock()

    @property
    def is_armed(self) -> bool:
        with self._state_lock:
            return self._armed

    def start(self) -> None:
        if self._running:
            return
        self._discard_stale_events()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise HardwareMonitorUnavailable(
                "start the GPIO monitor from the FastAPI event loop"
            ) from exc

        self._instance_lock.acquire()
        try:
            button = self._button_factory(
                self.pin,
                pull_up=True,
                bounce_time=self.debounce_seconds,
                hold_time=self.hold_seconds,
                hold_repeat=False,
            )
        except Exception:
            self._instance_lock.release()
            raise

        with self._state_lock:
            self._loop = loop
            self._button = button
            self._running = True
            self._fired_for_press = False
            self._armed = not bool(button.is_pressed)
            button.when_held = self._on_held
            button.when_released = self._on_released

    async def get_event(self) -> HardwareActivationEvent:
        item = await self._queue.get()
        if item is None:
            raise HardwareMonitorStopped("GPIO joystick monitor is stopped")
        return item

    def stop(self) -> None:
        with self._state_lock:
            if not self._running:
                return
            self._running = False
            self._armed = False
            button = self._button
            loop = self._loop
            self._button = None
            self._loop = None
            if button is not None:
                button.when_held = None
                button.when_released = None

        try:
            if button is not None:
                button.close()
        finally:
            self._instance_lock.release()
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._enqueue, None)

    def close(self) -> None:
        self.stop()

    def _on_held(self) -> None:
        with self._state_lock:
            if not self._running or not self._armed or self._fired_for_press:
                return
            self._fired_for_press = True
            self._armed = False
            loop = self._loop
        if loop is None or loop.is_closed():
            return
        event = HardwareActivationEvent(
            event_id=f"gpio-{uuid4()}",
            device_id=self.device_id,
            occurred_at=datetime.now(UTC),
        )
        # gpiozero callbacks may run in a worker thread. The only callback work
        # is scheduling an in-memory queue write on the FastAPI event loop.
        loop.call_soon_threadsafe(self._enqueue, event)

    def _on_released(self) -> None:
        with self._state_lock:
            if not self._running:
                return
            self._fired_for_press = False
            self._armed = True

    def _enqueue(self, item: HardwareActivationEvent | None) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # A held input cannot block a gpiozero callback. The session layer
            # also enforces one active session, so dropping excess events is safe.
            return

    def _discard_stale_events(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return


def _gpiozero_button_factory(*args: Any, **kwargs: Any) -> ButtonLike:
    try:
        from gpiozero import Button
    except ImportError as exc:  # pragma: no cover - depends on Raspberry Pi extra
        raise HardwareMonitorUnavailable(
            "gpiozero is not installed; install the Raspberry Pi dependency extra"
        ) from exc
    return Button(*args, **kwargs)
