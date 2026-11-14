from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.container import build_services
from app.safety import AlertReason, SafetySession, SessionState
from app.services.alerts import (
    AlertIncidentType,
    AlertRecord,
    AlertStatus,
)
from app.services.recovery import (
    RecoveredCommand,
    RecoveredSessionBundle,
    StaticSessionRecoveryProvider,
)
from app.services.session_manager import ActivationOutcome

NOW = datetime(2026, 11, 14, 18, 0, tzinfo=UTC)
SECRET = "recovery-test-signing-secret-with-at-least-32-bytes"


def _settings() -> Settings:
    return Settings(
        control_signing_secret=SECRET,
        control_secret_is_ephemeral=False,
        voice_mode="demo",
        scheduler_enabled=False,
        gpio_mode="disabled",
    )


def _alerting_session(session_id: str = "recovered-session") -> SafetySession:
    return SafetySession(
        session_id=session_id,
        device_id="vesta-rpi-01",
        state=SessionState.ALERT,
        version=8,
        created_at=NOW - timedelta(minutes=5),
        activated_at=NOW - timedelta(minutes=5),
        updated_at=NOW - timedelta(seconds=10),
        alert_reason=AlertReason.SOS_EXPLICIT,
    )


def _active_alert(session_id: str = "recovered-session") -> AlertRecord:
    created_at = NOW - timedelta(seconds=10)
    return AlertRecord(
        alert_id="persisted-alert-id",
        session_id=session_id,
        incident_type=AlertIncidentType.EXPLICIT_SOS,
        status=AlertStatus.ACTIVE,
        created_at=created_at,
        updated_at=created_at,
        notification_requested_at=created_at,
        version=3,
    )


def _fingerprint(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def test_recovery_preserves_alert_identity_and_idempotency_record() -> None:
    session = _alerting_session()
    bundle = RecoveredSessionBundle(
        session=session,
        alert=_active_alert(),
        processed_commands=(
            RecoveredCommand(
                event_id="persisted-activation-event",
                fingerprint=_fingerprint("activate", session.device_id),
                session_id=session.session_id,
            ),
        ),
    )
    services = build_services(
        _settings(),
        recovery_provider=StaticSessionRecoveryProvider([bundle]),
    )

    async def scenario() -> None:
        recovered = await services.sessions.recover_unfinished(now=NOW)
        assert [item.session_id for item in recovered] == [session.session_id]
        restored_alert = services.alerts.get_for_session(session.session_id)
        assert restored_alert is not None
        assert restored_alert.alert_id == "persisted-alert-id"
        assert restored_alert.version == 3

        replay = await services.sessions.activate(
            event_id="persisted-activation-event",
            device_id=session.device_id,
            now=NOW + timedelta(seconds=1),
        )
        assert replay.outcome is ActivationOutcome.DUPLICATE
        assert replay.session.session_id == session.session_id

        events = await services.broker.recent(limit=20)
        assert [event.type for event in events] == ["session.recovered"]
        await services.voice.aclose()

    asyncio.run(scenario())


def test_recovery_rejects_multiple_unfinished_sessions() -> None:
    first = _alerting_session("session-one")
    second = SafetySession(
        session_id="session-two",
        device_id="vesta-rpi-01",
        state=SessionState.ACTIVE,
        created_at=NOW,
        activated_at=NOW,
        updated_at=NOW,
    )
    services = build_services(
        _settings(),
        recovery_provider=StaticSessionRecoveryProvider([first, second]),
    )

    with pytest.raises(RuntimeError, match="multiple unfinished sessions"):
        asyncio.run(services.sessions.recover_unfinished(now=NOW))
    asyncio.run(services.voice.aclose())


@pytest.mark.parametrize(
    "alert",
    [
        _active_alert("another-session"),
        AlertRecord(
            alert_id="resolved-alert",
            session_id="recovered-session",
            incident_type=AlertIncidentType.EXPLICIT_SOS,
            status=AlertStatus.RESOLVED,
            created_at=NOW - timedelta(seconds=20),
            updated_at=NOW - timedelta(seconds=5),
            notification_requested_at=NOW - timedelta(seconds=20),
            version=2,
            resolved_at=NOW - timedelta(seconds=5),
            resolution_action="USER_CONFIRMED_SAFE",
        ),
    ],
)
def test_recovery_rejects_alert_session_or_state_mismatch(alert: AlertRecord) -> None:
    bundle = RecoveredSessionBundle(
        session=_alerting_session(),
        alert=alert,
    )
    services = build_services(
        _settings(),
        recovery_provider=StaticSessionRecoveryProvider([bundle]),
    )

    with pytest.raises(RuntimeError, match="Recovered alert"):
        asyncio.run(services.sessions.recover_unfinished(now=NOW))
    asyncio.run(services.voice.aclose())
