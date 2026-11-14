from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.container import build_services
from app.main import create_app
from app.safety import SessionState, TechnicalIssue

SIGNING_SECRET = "test-control-signing-secret-with-more-than-thirty-two-bytes"
DEVICE_KEY = "test-device-key"
BASE_TIME = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "control_signing_secret": SIGNING_SECRET,
        "control_secret_is_ephemeral": False,
        "device_api_key": DEVICE_KEY,
        "voice_mode": "demo",
        "scheduler_enabled": False,
        "checkin_interval_seconds": 10.0,
        "checkin_response_timeout_seconds": 4.0,
        "checkin_retry_delay_seconds": 2.0,
        "checkin_max_attempts": 2,
        "websocket_heartbeat_seconds": 2.0,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def client() -> TestClient:
    app = create_app(make_settings())
    with TestClient(app) as test_client:
        yield test_client


def activate(
    client: TestClient,
    *,
    event_id: str = "activate-1",
    device_id: str = "vesta-01",
) -> dict[str, object]:
    response = client.post(
        "/api/v1/device/events",
        headers={"X-Device-Key": DEVICE_KEY},
        json={
            "event_id": event_id,
            "device_id": device_id,
            "event": "activate",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def bearer(token: object) -> dict[str, str]:
    assert isinstance(token, str)
    return {"Authorization": f"Bearer {token}"}


def test_health_and_empty_status(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "service": "vesta-backend",
        "voice_mode": "demo",
    }

    status_response = client.get("/api/v1/status")
    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["system_state"] == "IDLE"
    assert payload["sessions"] == []


def test_device_key_is_required_when_configured(client: TestClient) -> None:
    payload = {
        "event_id": "activate-unauthorized",
        "device_id": "vesta-01",
        "event": "activate",
    }
    missing = client.post("/api/v1/device/events", json=payload)
    wrong = client.post(
        "/api/v1/device/events",
        headers={"X-Device-Key": "wrong"},
        json=payload,
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.json()["error"]["code"] == "UNAUTHORIZED"


def test_activation_is_idempotent_and_rejects_event_id_reuse(client: TestClient) -> None:
    first = activate(client)
    duplicate = client.post(
        "/api/v1/device/events",
        headers={"X-Device-Key": DEVICE_KEY},
        json={
            "event_id": "activate-1",
            "device_id": "vesta-01",
            "event": "activate",
        },
    )

    assert duplicate.status_code == 200
    duplicate_payload = duplicate.json()
    assert duplicate_payload["duplicate"] is True
    assert duplicate_payload["session"]["id"] == first["session"]["id"]

    reused = client.post(
        "/api/v1/device/events",
        headers={"X-Device-Key": DEVICE_KEY},
        json={
            "event_id": "activate-1",
            "device_id": "vesta-02",
            "event": "activate",
        },
    )
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "EVENT_ID_REUSED"


def test_invalid_session_token_is_rejected_before_command(client: TestClient) -> None:
    activation = activate(client)
    session_id = activation["session"]["id"]
    response = client.post(
        f"/api/v1/sessions/{session_id}/checkins",
        headers={"Authorization": "Bearer invalid-token"},
        json={
            "event_id": "checkin-unauthorized",
            "checkin_id": "checkin-does-not-matter",
            "confirmation": "safe",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_normal_checkin_returns_session_to_active(client: TestClient) -> None:
    activation = activate(client)
    session_id = activation["session"]["id"]
    manager = client.app.state.services.sessions
    initial = asyncio.run(manager.get(session_id))
    assert initial.next_check_in_at is not None

    asyncio.run(manager.advance_all(now=initial.next_check_in_at))
    checkin = asyncio.run(manager.get(session_id))
    assert checkin.current_checkin_id is not None
    delivered = client.post(
        f"/api/v1/sessions/{session_id}/checkins/{checkin.current_checkin_id}/delivery",
        headers=bearer(activation["control_token"]),
        json={"event_id": "delivery-1", "status": "DELIVERED", "channel": "voice"},
    )
    assert delivered.status_code == 200
    response = client.post(
        f"/api/v1/sessions/{session_id}/checkins",
        headers=bearer(activation["control_token"]),
        json={
            "event_id": "checkin-safe-1",
            "checkin_id": checkin.current_checkin_id,
            "confirmation": "safe",
            "source": "android",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["duplicate"] is False
    assert payload["session"]["state"] == "ACTIVE"
    assert payload["session"]["checkin_attempt"] == 0
    assert payload["session"]["alert_reason"] is None


def test_timeouts_retry_once_then_raise_deterministic_alert() -> None:
    services = build_services(make_settings(device_api_key=None))

    async def scenario() -> None:
        result = await services.sessions.activate(
            event_id="timed-activate",
            device_id="vesta-timed",
            now=BASE_TIME,
        )
        session_id = result.session.session_id

        first = await services.sessions.advance_all(now=BASE_TIME + timedelta(seconds=10))
        assert [transition.kind.value for transition in first] == ["check_in_started"]
        first_checkin = await services.sessions.get(session_id)
        assert first_checkin.state is SessionState.CHECK_IN
        assert first_checkin.current_checkin_id is not None
        await services.sessions.mark_checkin_delivery(
            session_id,
            first_checkin.current_checkin_id,
            token=result.control_token,
            event_id="timed-delivery-1",
            delivered=True,
            channel="voice",
            now=BASE_TIME + timedelta(seconds=11),
        )

        missed = await services.sessions.advance_all(now=BASE_TIME + timedelta(seconds=14))
        assert [transition.kind.value for transition in missed] == ["check_in_missed"]
        assert (await services.sessions.get(session_id)).state is SessionState.UNCONFIRMED

        retry = await services.sessions.advance_all(now=BASE_TIME + timedelta(seconds=16))
        assert [transition.kind.value for transition in retry] == ["check_in_started"]
        second_checkin = await services.sessions.get(session_id)
        assert second_checkin.current_checkin_id is not None
        await services.sessions.mark_checkin_delivery(
            session_id,
            second_checkin.current_checkin_id,
            token=result.control_token,
            event_id="timed-delivery-2",
            delivered=True,
            channel="voice",
            now=BASE_TIME + timedelta(seconds=17),
        )

        alerted = await services.sessions.advance_all(now=BASE_TIME + timedelta(seconds=20))
        assert [transition.kind.value for transition in alerted] == ["alerted"]
        final = await services.sessions.get(session_id)
        assert final.state is SessionState.ALERT
        assert final.alert_reason is not None
        assert final.alert_reason.value == "MISSED_CHECK_INS"

        event_types = [event.type for event in await services.broker.recent()]
        assert event_types == [
            "session.activated",
            "safety.check_in_requested",
            "safety.check_in_delivered",
            "safety.check_in_unconfirmed",
            "safety.check_in_requested",
            "safety.check_in_delivered",
            "safety.alerted",
            "alert.notification_requested",
        ]
        await services.voice.aclose()

    asyncio.run(scenario())


def test_sos_requires_explicit_alert_resolution_before_new_session(
    client: TestClient,
) -> None:
    activation = activate(client)
    session_id = activation["session"]["id"]
    headers = bearer(activation["control_token"])

    sos = client.post(
        f"/api/v1/sessions/{session_id}/sos",
        headers=headers,
        json={
            "event_id": "sos-1",
            "source": "android",
            "reason": "Am nevoie de ajutor",
        },
    )
    assert sos.status_code == 200
    assert sos.json()["session"]["state"] == "ALERT"
    sos_session = sos.json()["session"]
    assert sos_session["alert_reason"] == "SOS_EXPLICIT"
    alert_id = sos_session["alert"]["id"]

    cancelled = client.post(
        f"/api/v1/sessions/{session_id}/cancel",
        headers=headers,
        json={"event_id": "cancel-1", "reason": "USER_CANCELLED"},
    )
    assert cancelled.status_code == 409
    assert cancelled.json()["error"]["code"] == "ALERT_REQUIRES_RESOLUTION"

    resolved = client.post(
        f"/api/v1/sessions/{session_id}/alerts/{alert_id}/resolve",
        headers=headers,
        json={"event_id": "resolve-1", "action": "USER_CONFIRMED_SAFE"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["session"]["state"] == "CANCELLED"
    assert resolved.json()["session"]["alert"]["status"] == "RESOLVED"
    assert client.get("/api/v1/status").json()["system_state"] == "IDLE"

    restarted = activate(client, event_id="activate-2")
    assert restarted["session"]["id"] != session_id
    assert restarted["session"]["state"] == "ACTIVE"


def test_technical_issue_defers_timeouts_without_alerting() -> None:
    services = build_services(make_settings(device_api_key=None))

    async def scenario() -> None:
        activation = await services.sessions.activate(
            event_id="technical-activate",
            device_id="vesta-technical",
            now=BASE_TIME,
        )
        session_id = activation.session.session_id
        await services.sessions.set_technical_issue(
            session_id,
            token=activation.control_token,
            event_id="network-down",
            issue=TechnicalIssue.NETWORK_DISCONNECTED,
            active=True,
            source="android",
            now=BASE_TIME + timedelta(seconds=1),
        )

        transitions = await services.sessions.advance_all(now=BASE_TIME + timedelta(minutes=5))
        assert [transition.kind.value for transition in transitions] == ["check_in_deferred"]
        deferred = await services.sessions.get(session_id)
        assert deferred.state is SessionState.ACTIVE
        assert deferred.alert_reason is None

        await services.sessions.set_technical_issue(
            session_id,
            token=activation.control_token,
            event_id="network-restored",
            issue=TechnicalIssue.NETWORK_DISCONNECTED,
            active=False,
            source="android",
            now=BASE_TIME + timedelta(minutes=5, seconds=1),
        )
        started = await services.sessions.advance_all(
            now=BASE_TIME + timedelta(minutes=5, seconds=6)
        )
        assert [transition.kind.value for transition in started] == ["check_in_started"]
        assert (await services.sessions.get(session_id)).state is SessionState.CHECK_IN
        await services.voice.aclose()

    asyncio.run(scenario())


def test_expired_authenticated_heartbeat_becomes_network_issue_not_alert() -> None:
    services = build_services(
        make_settings(
            device_api_key=None,
            client_heartbeat_timeout_seconds=5.0,
        )
    )

    async def scenario() -> None:
        activation = await services.sessions.activate(
            event_id="heartbeat-activate",
            device_id="vesta-heartbeat",
            now=BASE_TIME,
        )
        session_id = activation.session.session_id
        heartbeat = await services.sessions.heartbeat(
            session_id,
            token=activation.control_token,
            now=BASE_TIME + timedelta(seconds=1),
        )
        assert heartbeat.session.connection_lease_expires_at == BASE_TIME + timedelta(seconds=6)

        transitions = await services.sessions.advance_all(now=BASE_TIME + timedelta(seconds=10))
        assert [transition.kind.value for transition in transitions] == [
            "technical_issue_reported",
            "check_in_deferred",
        ]
        disconnected = await services.sessions.get(session_id)
        assert disconnected.state is SessionState.ACTIVE
        assert TechnicalIssue.NETWORK_DISCONNECTED in disconnected.technical_issues
        assert disconnected.alert_reason is None

        restored = await services.sessions.heartbeat(
            session_id,
            token=activation.control_token,
            now=BASE_TIME + timedelta(seconds=11),
        )
        assert TechnicalIssue.NETWORK_DISCONNECTED not in restored.session.technical_issues
        await services.voice.aclose()

    asyncio.run(scenario())


def test_heartbeat_endpoint_registers_connectivity_lease(client: TestClient) -> None:
    activation = activate(client)
    session_id = activation["session"]["id"]
    response = client.post(
        f"/api/v1/sessions/{session_id}/heartbeat",
        headers=bearer(activation["control_token"]),
        json={},
    )

    assert response.status_code == 200
    session = response.json()["session"]
    assert session["client_last_seen_at"] is not None
    assert session["connection_lease_expires_at"] is not None


def test_demo_voice_access_is_explicitly_marked_and_contains_romanian_policy(
    client: TestClient,
) -> None:
    activation = activate(client)
    session_id = activation["session"]["id"]
    response = client.post(
        f"/api/v1/sessions/{session_id}/voice/access",
        headers=bearer(activation["control_token"]),
        json={"transport": "webrtc", "participant_name": "Demo"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["provider"] == "demo"
    assert payload["mode"] == "demo"
    assert payload["transport"] == "scripted"
    assert payload["conversation_token"] is None
    assert payload["client_config"]["language"] == "ro"
    assert "vesta_confirm_safe" in {
        tool["name"] for tool in payload["client_config"]["client_tools"]
    }
    session = client.get("/api/v1/status").json()["sessions"][0]
    assert session["connection_lease_expires_at"] is None


def test_unconfigured_live_voice_is_technical_failure_not_safety_alert() -> None:
    settings = make_settings(
        voice_mode="elevenlabs",
        elevenlabs_api_key=None,
        elevenlabs_agent_id=None,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        activation = activate(client)
        session_id = activation["session"]["id"]
        response = client.post(
            f"/api/v1/sessions/{session_id}/voice/access",
            headers=bearer(activation["control_token"]),
            json={"transport": "webrtc"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "VOICE_NOT_CONFIGURED"

        session = client.get("/api/v1/status").json()["sessions"][0]
        assert session["state"] == "ACTIVE"
        assert session["alert_reason"] is None
        assert session["active_technical_issues"] == ["voice_unavailable"]


def test_websocket_starts_with_snapshot_then_streams_transition(client: TestClient) -> None:
    with client.websocket_connect("/ws/events") as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["type"] == "system.snapshot"
        assert snapshot["data"]["system_state"] == "IDLE"

        activation = activate(client, event_id="ws-activate")
        event = websocket.receive_json()
        assert event["type"] == "session.activated"
        assert event["session_id"] == activation["session"]["id"]
        assert event["data"]["state"] == "ACTIVE"


def test_websocket_rejects_unlisted_browser_origin(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            "/ws/events", headers={"Origin": "https://untrusted.example"}
        ):
            pass
    assert error.value.code == 1008


def test_websocket_transport_heartbeat_does_not_advance_sequence() -> None:
    app = create_app(make_settings(websocket_heartbeat_seconds=0.05))
    with TestClient(app) as client:
        with client.websocket_connect("/ws/events") as websocket:
            snapshot = websocket.receive_json()
            heartbeat = websocket.receive_json()

    assert snapshot["type"] == "system.snapshot"
    assert heartbeat["type"] == "system.heartbeat"
    assert heartbeat["sequence"] == 0


def test_websocket_snapshot_uses_watermark_and_sos_does_not_broadcast_reason(
    client: TestClient,
) -> None:
    activation = activate(client, event_id="ws-existing")
    session_id = activation["session"]["id"]

    with client.websocket_connect("/ws/events") as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["type"] == "system.snapshot"
        assert snapshot["sequence"] == 1
        assert snapshot["data"]["latest_sequence"] == 1
        assert snapshot["data"]["system_state"] == "ACTIVE"

        response = client.post(
            f"/api/v1/sessions/{session_id}/sos",
            headers=bearer(activation["control_token"]),
            json={
                "event_id": "ws-sos",
                "source": "android",
                "reason": "text sensibil care nu trebuie difuzat",
            },
        )
        assert response.status_code == 200
        event = websocket.receive_json()
        assert event["type"] == "safety.alerted"
        assert event["sequence"] == 2
        assert "user_reason" not in event["data"]


def test_openapi_does_not_contain_permanent_secrets(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    document = response.text
    assert SIGNING_SECRET not in document
    assert DEVICE_KEY not in document
    assert "ELEVENLABS_API_KEY" not in document
    assert "/api/v1/telemetry" not in document
    assert "/api/v1/location" not in document
    assert set(response.json()["paths"]) == {
        "/health",
        "/api/v1/status",
        "/api/v1/device/events",
        "/api/v1/sessions/{session_id}/checkins",
        "/api/v1/sessions/{session_id}/checkins/{checkin_id}/delivery",
        "/api/v1/sessions/{session_id}",
        "/api/v1/sessions/{session_id}/sos",
        "/api/v1/sessions/{session_id}/cancel",
        "/api/v1/sessions/{session_id}/alerts/{alert_id}/resolve",
        "/api/v1/sessions/{session_id}/heartbeat",
        "/api/v1/sessions/{session_id}/client-status",
        "/api/v1/sessions/{session_id}/location",
        "/api/v1/sessions/{session_id}/technical-status",
        "/api/v1/sessions/{session_id}/voice/access",
        "/api/v1/sessions/{session_id}/voice/status",
        "/api/v1/pairing-codes",
        "/api/v1/pairing/claim",
        "/api/v1/companion/session",
        "/api/v1/contact/status",
        "/api/v1/contact/alerts/{alert_id}/viewed",
    }


def test_invalid_and_extra_payload_fields_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/device/events",
        headers={"X-Device-Key": DEVICE_KEY},
        json={
            "event_id": "invalid-activate",
            "device_id": "vesta-01",
            "event": "activate",
            "unexpected": "field",
        },
    )
    assert response.status_code == 422

    wrong_event = client.post(
        "/api/v1/device/events",
        headers={"X-Device-Key": DEVICE_KEY},
        json={
            "event_id": "invalid-event",
            "device_id": "vesta-01",
            "event": "sos",
        },
    )
    assert wrong_event.status_code == 422
