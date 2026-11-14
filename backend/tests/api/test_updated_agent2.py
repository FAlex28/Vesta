from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.container import build_services
from app.errors import ConflictError
from app.main import create_app
from app.safety import (
    AlertReason,
    AudioStatus,
    SafetySession,
    SessionState,
    TechnicalIssue,
)
from app.services.alerts import AlertIncidentType, AlertStatus
from app.services.recovery import StaticSessionRecoveryProvider
from app.services.session_manager import ActivationOutcome

SIGNING_SECRET = "updated-agent2-signing-secret-with-more-than-thirty-two-bytes"
DEVICE_KEY = "updated-agent2-device-key"
DASHBOARD_KEY = "updated-agent2-dashboard-key"
BASE_TIME = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "control_signing_secret": SIGNING_SECRET,
        "control_secret_is_ephemeral": False,
        "device_api_key": DEVICE_KEY,
        "voice_mode": "demo",
        "scheduler_enabled": False,
        "gpio_mode": "disabled",
        "checkin_interval_seconds": 10.0,
        "checkin_response_timeout_seconds": 4.0,
        "checkin_retry_delay_seconds": 2.0,
        "checkin_max_attempts": 2,
    }
    values.update(overrides)
    return Settings(**values)


def activate(client: TestClient, event_id: str = "activate-1") -> dict[str, object]:
    response = client.post(
        "/api/v1/device/events",
        headers={"X-Device-Key": DEVICE_KEY},
        json={"event_id": event_id, "device_id": "vesta-rpi-01", "event": "activate"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth(token: object) -> dict[str, str]:
    assert isinstance(token, str)
    return {"Authorization": f"Bearer {token}"}


def test_repress_returns_already_active_without_second_session() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        first = activate(client)
        repeated = client.post(
            "/api/v1/device/events",
            headers={"X-Device-Key": DEVICE_KEY},
            json={
                "event_id": "activate-repress",
                "device_id": "another-payload-device",
                "event": "activate",
            },
        )

        assert repeated.status_code == 200
        body = repeated.json()
        assert body["outcome"] == "ALREADY_ACTIVE"
        assert body["duplicate"] is False
        assert body["session"]["id"] == first["session"]["id"]
        assert len(client.get("/api/v1/status").json()["sessions"]) == 1


def test_quick_cancel_records_reason_and_keeps_activation_history() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        activation = activate(client)
        session_id = activation["session"]["id"]
        response = client.post(
            f"/api/v1/sessions/{session_id}/cancel",
            headers=auth(activation["control_token"]),
            json={"event_id": "quick-cancel", "reason": "ACCIDENTAL_ACTIVATION"},
        )

        assert response.status_code == 200
        session = response.json()["session"]
        assert session["state"] == "CANCELLED"
        assert session["cancellation_reason"] == "ACCIDENTAL_ACTIVATION"
        assert session["created_at"] is not None
        assert session["version"] >= 2


def test_sos_in_quick_window_is_immediate_and_quick_cancel_resolves_alert() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        activation = activate(client)
        session_id = activation["session"]["id"]
        headers = auth(activation["control_token"])
        sos = client.post(
            f"/api/v1/sessions/{session_id}/sos",
            headers=headers,
            json={"event_id": "quick-sos", "source": "web"},
        )
        assert sos.status_code == 200
        assert sos.json()["session"]["state"] == "ALERT"
        assert sos.json()["session"]["alert_reason"] == "SOS_EXPLICIT"

        cancelled = client.post(
            f"/api/v1/sessions/{session_id}/cancel",
            headers=headers,
            json={"event_id": "quick-alert-cancel", "reason": "ACCIDENTAL_ACTIVATION"},
        )
        assert cancelled.status_code == 200
        session = cancelled.json()["session"]
        assert session["state"] == "CANCELLED"
        assert session["cancellation_reason"] == "ACCIDENTAL_ACTIVATION"
        assert session["alert"]["status"] == "RESOLVED"
        assert session["alert"]["resolution_action"] == "ACCIDENTAL_ACTIVATION"


def test_undelivered_audio_checkin_can_be_confirmed_from_web() -> None:
    services = build_services(settings(device_api_key=None))

    async def scenario() -> None:
        activation = await services.sessions.activate(
            event_id="fallback-activation",
            device_id="vesta-rpi-01",
            now=BASE_TIME,
        )
        await services.sessions.advance_all(now=BASE_TIME + timedelta(seconds=10))
        checkin = await services.sessions.get(activation.session.session_id)
        assert checkin.current_checkin_id is not None
        await services.sessions.mark_checkin_delivery(
            checkin.session_id,
            checkin.current_checkin_id,
            token=activation.control_token,
            event_id="fallback-undelivered",
            delivered=False,
            channel="voice",
            now=BASE_TIME + timedelta(seconds=11),
        )
        result = await services.sessions.confirm_safe(
            checkin.session_id,
            token=activation.control_token,
            event_id="fallback-web-confirm",
            checkin_id=checkin.current_checkin_id,
            source="web",
            now=BASE_TIME + timedelta(seconds=12),
        )
        assert result.session.state is SessionState.ACTIVE
        assert result.session.last_confirmation_at == BASE_TIME + timedelta(seconds=12)
        assert result.session.last_checkin_id == checkin.current_checkin_id
        assert TechnicalIssue.AUDIO_DISCONNECTED in result.session.technical_issues
        await services.voice.aclose()

    asyncio.run(scenario())


def test_client_statuses_are_technical_and_do_not_block_sos() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        activation = activate(client)
        session_id = activation["session"]["id"]
        headers = auth(activation["control_token"])
        update = client.post(
            f"/api/v1/sessions/{session_id}/client-status",
            headers=headers,
            json={
                "event_id": "all-client-statuses",
                "page_visibility": "HIDDEN",
                "connectivity_status": "DISCONNECTED",
                "audio_status": "DISCONNECTED",
                "gps_status": "GPS_UNAVAILABLE",
                "power_status": "POWER_UNSTABLE",
            },
        )
        assert update.status_code == 200, update.text
        session = update.json()["session"]
        assert session["state"] == "ACTIVE"
        assert session["health_status"] == "DISCONNECTED"
        assert session["gps_status"] == "GPS_UNAVAILABLE"
        assert session["power_status"] == "POWER_UNSTABLE"
        assert "battery_percentage" not in session

        sos = client.post(
            f"/api/v1/sessions/{session_id}/sos",
            headers=headers,
            json={"event_id": "sos-despite-technical-failures", "source": "android"},
        )
        assert sos.status_code == 200
        assert sos.json()["session"]["state"] == "ALERT"


def test_timeout_and_sos_converge_on_one_alert_and_notification() -> None:
    services = build_services(settings(device_api_key=None))

    async def scenario() -> None:
        activation = await services.sessions.activate(
            event_id="concurrent-activation",
            device_id="vesta-rpi-01",
            now=BASE_TIME,
        )
        session_id = activation.session.session_id
        await services.sessions.advance_all(now=BASE_TIME + timedelta(seconds=10))
        first = await services.sessions.get(session_id)
        assert first.current_checkin_id is not None
        await services.sessions.mark_checkin_delivery(
            session_id,
            first.current_checkin_id,
            token=activation.control_token,
            event_id="concurrent-delivery-1",
            delivered=True,
            channel="voice",
            now=BASE_TIME + timedelta(seconds=11),
        )
        await services.sessions.advance_all(now=BASE_TIME + timedelta(seconds=14))
        await services.sessions.advance_all(now=BASE_TIME + timedelta(seconds=16))
        second = await services.sessions.get(session_id)
        assert second.current_checkin_id is not None
        await services.sessions.mark_checkin_delivery(
            session_id,
            second.current_checkin_id,
            token=activation.control_token,
            event_id="concurrent-delivery-2",
            delivered=True,
            channel="voice",
            now=BASE_TIME + timedelta(seconds=17),
        )
        await services.sessions.advance_all(now=BASE_TIME + timedelta(seconds=20))
        timeout_alert = services.alerts.get_for_session(session_id)
        assert timeout_alert is not None

        sos = await services.sessions.request_sos(
            session_id,
            token=activation.control_token,
            event_id="concurrent-sos",
            source="android",
            reason=None,
            now=BASE_TIME + timedelta(seconds=20, milliseconds=1),
        )
        assert sos.alert is not None
        assert sos.alert.alert_id == timeout_alert.alert_id
        assert sos.alert.incident_type is AlertIncidentType.EXPLICIT_SOS
        assert len(services.alerts.list()) == 1
        notifications = [
            event
            for event in await services.broker.recent(limit=100)
            if event.type == "alert.notification_requested"
        ]
        assert len(notifications) == 1
        await services.voice.aclose()

    asyncio.run(scenario())


def test_restart_recovers_same_session_and_does_not_assume_audio() -> None:
    persisted = SafetySession(
        session_id="persisted-session",
        device_id="vesta-rpi-01",
        state=SessionState.ACTIVE,
        version=7,
        created_at=BASE_TIME,
        activated_at=BASE_TIME,
        updated_at=BASE_TIME,
        next_check_in_at=BASE_TIME + timedelta(minutes=1),
        audio_status=AudioStatus.CONNECTED,
    )
    services = build_services(
        settings(device_api_key=None),
        recovery_provider=StaticSessionRecoveryProvider([persisted]),
    )

    async def scenario() -> None:
        recovered = await services.sessions.recover_unfinished(now=BASE_TIME + timedelta(minutes=2))
        assert [session.session_id for session in recovered] == ["persisted-session"]
        current = await services.sessions.get("persisted-session")
        assert current.state is SessionState.ACTIVE
        assert current.audio_status is AudioStatus.DISCONNECTED
        assert TechnicalIssue.PROCESS_RESTARTED in current.technical_issues
        assert TechnicalIssue.VOICE_UNAVAILABLE in current.technical_issues
        assert current.version == 8
        assert len(await services.sessions.list_sessions()) == 1
        events = await services.broker.recent()
        assert events[-1].type == "session.recovered"
        await services.voice.aclose()

    asyncio.run(scenario())


def test_late_confirmation_cannot_change_timed_out_checkin() -> None:
    services = build_services(settings(device_api_key=None))

    async def scenario() -> None:
        activation = await services.sessions.activate(
            event_id="late-activation",
            device_id="vesta-rpi-01",
            now=BASE_TIME,
        )
        session_id = activation.session.session_id
        await services.sessions.advance_all(now=BASE_TIME + timedelta(seconds=10))
        checkin = await services.sessions.get(session_id)
        assert checkin.current_checkin_id is not None
        await services.sessions.mark_checkin_delivery(
            session_id,
            checkin.current_checkin_id,
            token=activation.control_token,
            event_id="late-delivery",
            delivered=True,
            channel="voice",
            now=BASE_TIME + timedelta(seconds=11),
        )
        await services.sessions.advance_all(now=BASE_TIME + timedelta(seconds=14))
        with pytest.raises(ConflictError) as error:
            await services.sessions.confirm_safe(
                session_id,
                token=activation.control_token,
                event_id="late-confirmation",
                checkin_id=checkin.current_checkin_id,
                source="web",
                now=BASE_TIME + timedelta(seconds=14, milliseconds=1),
            )
        assert error.value.code == "CHECKIN_EXPIRED"
        assert (await services.sessions.get(session_id)).state is SessionState.UNCONFIRMED
        await services.voice.aclose()

    asyncio.run(scenario())


def test_dashboard_and_session_reads_are_authorized_and_scoped() -> None:
    app = create_app(settings(dashboard_api_key=DASHBOARD_KEY))
    with TestClient(app) as client:
        assert client.get("/api/v1/status").status_code == 401
        assert client.get("/api/v1/status", headers={"X-Dashboard-Key": "wrong"}).status_code == 401
        assert (
            client.get("/api/v1/status", headers={"X-Dashboard-Key": DASHBOARD_KEY}).status_code
            == 200
        )

        first = activate(client)
        first_id = first["session"]["id"]
        assert client.get(f"/api/v1/sessions/{first_id}").status_code == 401
        assert (
            client.get(
                f"/api/v1/sessions/{first_id}", headers=auth(first["control_token"])
            ).status_code
            == 200
        )
        client.post(
            f"/api/v1/sessions/{first_id}/cancel",
            headers=auth(first["control_token"]),
            json={"event_id": "finish-first", "reason": "USER_CANCELLED"},
        )
        second = activate(client, event_id="activate-second")
        second_id = second["session"]["id"]
        cross_read = client.get(
            f"/api/v1/sessions/{second_id}", headers=auth(first["control_token"])
        )
        assert cross_read.status_code == 401


def test_explicit_alert_resolution_is_retained_and_idempotent() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        activation = activate(client)
        session_id = activation["session"]["id"]
        headers = auth(activation["control_token"])
        sos = client.post(
            f"/api/v1/sessions/{session_id}/sos",
            headers=headers,
            json={"event_id": "resolve-sos", "source": "android"},
        ).json()
        alert_id = sos["session"]["alert"]["id"]
        payload = {"event_id": "resolve-alert", "action": "USER_CONFIRMED_SAFE"}
        first = client.post(
            f"/api/v1/sessions/{session_id}/alerts/{alert_id}/resolve",
            headers=headers,
            json=payload,
        )
        replay = client.post(
            f"/api/v1/sessions/{session_id}/alerts/{alert_id}/resolve",
            headers=headers,
            json=payload,
        )
        assert first.status_code == 200
        assert replay.status_code == 200
        assert replay.json()["duplicate"] is True
        alert = replay.json()["session"]["alert"]
        assert alert["status"] == AlertStatus.RESOLVED.value
        assert alert["resolved_at"] is not None
        assert alert["resolution_action"] == "USER_CONFIRMED_SAFE"


def test_activation_outcome_enum_remains_stable() -> None:
    assert {outcome.value for outcome in ActivationOutcome} == {
        "ACTIVATED",
        "DUPLICATE",
        "ALREADY_ACTIVE",
    }
    assert AlertReason.SOS_EXPLICIT.value == "SOS_EXPLICIT"
