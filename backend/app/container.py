from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.config import Settings
from app.hardware import (
    GpioZeroJoystickMonitor,
    HardwareActivationCoordinator,
    JoystickMonitor,
    MockJoystickMonitor,
    ProcessFileLock,
)
from app.realtime.broker import EventBroker
from app.safety import SafetyEngine, SafetyPolicy
from app.security import ControlTokenService
from app.services.access import AccessTokenService, AttemptLimiter
from app.services.alerts import AlertRegistry
from app.services.directory import DeviceDirectory
from app.services.location import (
    AuthorizedLocationStore,
    InMemoryAuthorizedLocationStore,
)
from app.services.recovery import NoopSessionRecoveryProvider, SessionRecoveryProvider
from app.services.session_manager import SessionManager
from app.voice import (
    DemoVoiceProvider,
    ElevenLabsVoiceProvider,
    VoiceConversationRegistry,
    VoiceProvider,
)


@dataclass(slots=True)
class AppServices:
    settings: Settings
    broker: EventBroker
    sessions: SessionManager
    alerts: AlertRegistry
    locations: AuthorizedLocationStore
    voice: VoiceProvider
    voice_sessions: VoiceConversationRegistry
    hardware_monitor: JoystickMonitor | None
    hardware_coordinator: HardwareActivationCoordinator | None
    access_tokens: AccessTokenService
    attempts: AttemptLimiter
    directory: DeviceDirectory
    # TigerPersistence când VESTA_PERSISTENCE=tiger; altfel None (doar în memorie)
    persistence: Any | None = None
    # Latest Raspberry Pi power reading (vcgencmd), shared with GET /api/v1/status.
    power: dict[str, Any] | None = None
    # Web Push transport; tests replace it. None = real pywebpush with the device's VAPID key.
    push_sender: Any | None = None


def build_services(
    settings: Settings,
    *,
    recovery_provider: SessionRecoveryProvider | None = None,
    hardware_monitor: JoystickMonitor | None = None,
    location_store: AuthorizedLocationStore | None = None,
) -> AppServices:
    broker = EventBroker(queue_size=settings.websocket_queue_size)
    policy = SafetyPolicy(
        check_in_interval=timedelta(seconds=settings.checkin_interval_seconds),
        response_timeout=timedelta(seconds=settings.checkin_response_timeout_seconds),
        retry_delay=timedelta(seconds=settings.checkin_retry_delay_seconds),
        technical_issue_retry_delay=timedelta(seconds=settings.checkin_retry_delay_seconds),
        quick_cancel_window=timedelta(seconds=settings.quick_cancel_seconds),
        max_attempts=settings.checkin_max_attempts,
    )
    engine = SafetyEngine(policy)
    persistence = None
    if settings.persistence_mode == "tiger":
        # import întârziat: modul memory nu are nevoie de pg8000 / Tiger Data
        from app.db import Database
        from app.integrations.runtime import TigerPersistence

        persistence = TigerPersistence(
            Database(),
            broker,
            recipients=settings.trusted_contact_ids,
            response_timeout_s=settings.checkin_response_timeout_seconds,
        )
    tokens = ControlTokenService(
        settings.control_signing_secret,
        default_ttl=timedelta(seconds=settings.control_token_ttl_seconds),
    )
    alerts = AlertRegistry()
    locations = location_store or InMemoryAuthorizedLocationStore()
    sessions = SessionManager(
        engine=engine,
        broker=broker,
        token_service=tokens,
        control_token_ttl_seconds=settings.control_token_ttl_seconds,
        scheduler_tick_seconds=settings.scheduler_tick_seconds,
        client_heartbeat_timeout_seconds=settings.client_heartbeat_timeout_seconds,
        alerts=alerts,
        recovery_provider=recovery_provider or NoopSessionRecoveryProvider(),
        location_provider=locations,
        recorder=persistence.recorder if persistence is not None else None,
    )
    if settings.voice_mode == "demo":
        voice: VoiceProvider = DemoVoiceProvider()
    else:
        voice = ElevenLabsVoiceProvider(
            api_key=settings.elevenlabs_api_key,
            agent_id=settings.elevenlabs_agent_id,
            timeout_seconds=settings.elevenlabs_timeout_seconds,
            max_retries=settings.elevenlabs_retry_count,
        )
    selected_monitor = hardware_monitor or _build_hardware_monitor(settings)
    hardware_coordinator = (
        HardwareActivationCoordinator(monitor=selected_monitor, manager=sessions)
        if selected_monitor is not None
        else None
    )
    return AppServices(
        settings=settings,
        broker=broker,
        sessions=sessions,
        alerts=alerts,
        locations=locations,
        voice=voice,
        voice_sessions=VoiceConversationRegistry(),
        hardware_monitor=selected_monitor,
        hardware_coordinator=hardware_coordinator,
        access_tokens=AccessTokenService(settings.control_signing_secret),
        attempts=AttemptLimiter(),
        directory=DeviceDirectory(
            Path(settings.state_dir) / "directory.json" if settings.state_dir else None,
            max_phones=settings.max_paired_phones,
        ),
        persistence=persistence,
    )


def _build_hardware_monitor(settings: Settings) -> JoystickMonitor | None:
    if settings.gpio_mode == "disabled":
        return None
    if settings.gpio_mode == "mock":
        return MockJoystickMonitor(
            device_id=settings.gpio_device_id,
            hold_seconds=settings.gpio_hold_seconds,
            debounce_seconds=settings.gpio_debounce_seconds,
        )
    return GpioZeroJoystickMonitor(
        pin=settings.gpio_pin,
        device_id=settings.gpio_device_id,
        hold_seconds=settings.gpio_hold_seconds,
        debounce_seconds=settings.gpio_debounce_seconds,
        instance_lock=ProcessFileLock(settings.gpio_lock_path),
    )
