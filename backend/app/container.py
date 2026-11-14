from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.config import Settings
from app.realtime.broker import EventBroker
from app.safety import SafetyEngine, SafetyPolicy
from app.security import ControlTokenService
from app.services.session_manager import SessionManager
from app.voice import DemoVoiceProvider, ElevenLabsVoiceProvider, VoiceProvider


@dataclass(slots=True)
class AppServices:
    settings: Settings
    broker: EventBroker
    sessions: SessionManager
    voice: VoiceProvider
    # TigerPersistence când VESTA_PERSISTENCE=tiger; altfel None (doar în memorie)
    persistence: Any | None = None


def build_services(settings: Settings) -> AppServices:
    broker = EventBroker(queue_size=settings.websocket_queue_size)
    policy = SafetyPolicy(
        check_in_interval=timedelta(seconds=settings.checkin_interval_seconds),
        response_timeout=timedelta(seconds=settings.checkin_response_timeout_seconds),
        retry_delay=timedelta(seconds=settings.checkin_retry_delay_seconds),
        technical_issue_retry_delay=timedelta(seconds=settings.checkin_retry_delay_seconds),
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
    sessions = SessionManager(
        engine=engine,
        broker=broker,
        token_service=tokens,
        control_token_ttl_seconds=settings.control_token_ttl_seconds,
        scheduler_tick_seconds=settings.scheduler_tick_seconds,
        client_heartbeat_timeout_seconds=settings.client_heartbeat_timeout_seconds,
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
    return AppServices(
        settings=settings,
        broker=broker,
        sessions=sessions,
        voice=voice,
        persistence=persistence,
    )
