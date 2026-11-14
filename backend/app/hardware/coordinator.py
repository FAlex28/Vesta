"""Async bridge from a joystick event stream to safety-session activation."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Protocol

from app.errors import ConflictError

from .base import HardwareActivationEvent, HardwareMonitorStopped, JoystickMonitor

logger = logging.getLogger(__name__)


class ActivationManager(Protocol):
    """Narrow session-manager contract needed by the hardware boundary."""

    async def activate(
        self,
        *,
        event_id: str,
        device_id: str,
        now: datetime | None = None,
    ) -> object: ...


class HardwareActivationCoordinator:
    """Consume accepted holds without coupling GPIO callbacks to external I/O.

    The monitor callback only places an event in memory. This coordinator runs
    on the application event loop and is therefore the layer allowed to invoke
    the asynchronous session service.
    """

    def __init__(
        self,
        *,
        monitor: JoystickMonitor,
        manager: ActivationManager,
        retry_delay_seconds: float = 0.25,
    ) -> None:
        if retry_delay_seconds <= 0:
            raise ValueError("retry_delay_seconds must be positive")
        self._monitor = monitor
        self._manager = manager
        self._retry_delay_seconds = retry_delay_seconds

    async def run(self, stop_event: asyncio.Event) -> None:
        """Forward hardware events until shutdown or monitor closure.

        A repeated physical activation can race with an already active safety
        session. Both the current and legacy semantic conflict codes are
        accepted as expected outcomes so the hardware consumer stays alive.
        """

        while not stop_event.is_set():
            try:
                event = await self._next_event(stop_event)
            except HardwareMonitorStopped:
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("hardware event read failed; retrying")
                if await self._wait_or_stop(stop_event):
                    return
                continue

            if event is None:
                return

            try:
                await self._manager.activate(
                    event_id=event.event_id,
                    device_id=event.device_id,
                    now=event.occurred_at,
                )
            except ConflictError as exc:
                if exc.code in {"ALREADY_ACTIVE", "SESSION_ALREADY_ACTIVE"}:
                    logger.info(
                        "hardware activation kept the existing safety session",
                        extra={"event_id": event.event_id, "device_id": event.device_id},
                    )
                    continue
                logger.exception("hardware activation conflict; retrying event stream")
                if await self._wait_or_stop(stop_event):
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("hardware activation failed; retrying event stream")
                if await self._wait_or_stop(stop_event):
                    return

    async def _next_event(self, stop_event: asyncio.Event) -> HardwareActivationEvent | None:
        event_task = asyncio.create_task(self._monitor.get_event())
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {event_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done and stop_task.result():
                if event_task.done():
                    # Observe any simultaneous monitor exception before leaving.
                    await asyncio.gather(event_task, return_exceptions=True)
                return None
            return await event_task
        finally:
            for task in (event_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(event_task, stop_task, return_exceptions=True)

    async def _wait_or_stop(self, stop_event: asyncio.Event) -> bool:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=self._retry_delay_seconds)
        except TimeoutError:
            return False
        return True
