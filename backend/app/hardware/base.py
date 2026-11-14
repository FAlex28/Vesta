"""Hardware activation boundary for the Vesta backend."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class HardwareActivationEvent:
    """One accepted physical activation, ready for the session service."""

    event_id: str
    device_id: str
    occurred_at: datetime
    source: str = "gpio"


class HardwareMonitorError(RuntimeError):
    """Base class for hardware monitor failures."""


class HardwareMonitorAlreadyRunning(HardwareMonitorError):
    """Another process or monitor already owns the GPIO input."""


class HardwareMonitorUnavailable(HardwareMonitorError):
    """The requested hardware backend is not available."""


class HardwareMonitorStopped(HardwareMonitorError):
    """The event stream was closed because the monitor stopped."""


@runtime_checkable
class JoystickMonitor(Protocol):
    """Common API implemented by Raspberry Pi and laptop adapters."""

    def start(self) -> None:
        """Acquire the input and begin monitoring it."""

    async def get_event(self) -> HardwareActivationEvent:
        """Wait for the next accepted hold activation."""

    def stop(self) -> None:
        """Stop monitoring and release every owned resource."""

    def close(self) -> None:
        """Alias for :meth:`stop` for lifecycle integrations."""
