"""Physical and simulated joystick activation adapters."""

from .base import (
    HardwareActivationEvent,
    HardwareMonitorAlreadyRunning,
    HardwareMonitorError,
    HardwareMonitorStopped,
    HardwareMonitorUnavailable,
    JoystickMonitor,
)
from .coordinator import ActivationManager, HardwareActivationCoordinator
from .gpiozero_adapter import GpioZeroJoystickMonitor, ProcessFileLock
from .mock import MockJoystickMonitor

__all__ = [
    "ActivationManager",
    "GpioZeroJoystickMonitor",
    "HardwareActivationEvent",
    "HardwareActivationCoordinator",
    "HardwareMonitorAlreadyRunning",
    "HardwareMonitorError",
    "HardwareMonitorStopped",
    "HardwareMonitorUnavailable",
    "JoystickMonitor",
    "MockJoystickMonitor",
    "ProcessFileLock",
]
