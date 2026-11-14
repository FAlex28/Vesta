from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

SIGNING_SECRET = "location-api-signing-secret-with-more-than-thirty-two-bytes"
DEVICE_KEY = "location-api-device-key"


def _settings() -> Settings:
    return Settings(
        environment="test",
        control_signing_secret=SIGNING_SECRET,
        control_secret_is_ephemeral=False,
        device_api_key=DEVICE_KEY,
        voice_mode="demo",
        scheduler_enabled=False,
        gpio_mode="disabled",
    )


def _activate(client: TestClient, event_id: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/device/events",
        headers={"X-Device-Key": DEVICE_KEY},
        json={
            "event_id": event_id,
            "device_id": "vesta-rpi-01",
            "event": "activate",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _auth(token: object) -> dict[str, str]:
    assert isinstance(token, str)
    return {"Authorization": f"Bearer {token}"}


def _payload(event_id: str, captured_at: datetime) -> dict[str, object]:
    return {
        "event_id": event_id,
        "latitude": 44.4268,
        "longitude": 26.1025,
        "captured_at": captured_at.isoformat(),
        "accuracy_meters": 8.0,
        "source": "android",
    }


def test_location_endpoint_requires_scoped_token_and_is_idempotent() -> None:
    app = create_app(_settings())
    with TestClient(app) as client:
        first = _activate(client, "activate-first")
        first_id = first["session"]["id"]
        now = datetime.now(UTC)
        location_payload = _payload("location-1", now)

        missing = client.post(
            f"/api/v1/sessions/{first_id}/location",
            json=location_payload,
        )
        invalid = client.post(
            f"/api/v1/sessions/{first_id}/location",
            headers={"Authorization": "Bearer invalid"},
            json=location_payload,
        )
        assert missing.status_code == 401
        assert invalid.status_code == 401

        accepted = client.post(
            f"/api/v1/sessions/{first_id}/location",
            headers=_auth(first["control_token"]),
            json=location_payload,
        )
        replay = client.post(
            f"/api/v1/sessions/{first_id}/location",
            headers=_auth(first["control_token"]),
            json=location_payload,
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["accepted_as_latest"] is True
        assert replay.status_code == 200
        assert replay.json()["duplicate"] is True

        reused = client.post(
            f"/api/v1/sessions/{first_id}/location",
            headers=_auth(first["control_token"]),
            json={**location_payload, "latitude": 45.0},
        )
        assert reused.status_code == 409
        assert reused.json()["error"]["code"] == "EVENT_ID_REUSED"

        cancelled = client.post(
            f"/api/v1/sessions/{first_id}/cancel",
            headers=_auth(first["control_token"]),
            json={"event_id": "cancel-first", "reason": "USER_CANCELLED"},
        )
        assert cancelled.status_code == 200
        second = _activate(client, "activate-second")
        second_id = second["session"]["id"]
        cross_session = client.post(
            f"/api/v1/sessions/{second_id}/location",
            headers=_auth(first["control_token"]),
            json=_payload("location-cross", now),
        )
        assert cross_session.status_code == 401


def test_alert_notification_uses_last_authorized_location_with_age_and_staleness() -> None:
    app = create_app(_settings())
    with TestClient(app) as client:
        activation = _activate(client, "activate-with-location")
        session_id = activation["session"]["id"]
        headers = _auth(activation["control_token"])
        captured_at = datetime.now(UTC) - timedelta(minutes=1)

        location = client.post(
            f"/api/v1/sessions/{session_id}/location",
            headers=headers,
            json=_payload("location-for-alert", captured_at),
        )
        assert location.status_code == 200, location.text
        gps = client.post(
            f"/api/v1/sessions/{session_id}/client-status",
            headers=headers,
            json={"event_id": "gps-stale", "gps_status": "GPS_STALE"},
        )
        assert gps.status_code == 200, gps.text
        sos = client.post(
            f"/api/v1/sessions/{session_id}/sos",
            headers=headers,
            json={"event_id": "sos-with-location", "source": "android"},
        )
        assert sos.status_code == 200, sos.text

        events = asyncio.run(app.state.services.broker.recent(limit=20))
        notification = next(
            event for event in events if event.type == "alert.notification_requested"
        )
        data = notification.data
        assert data["location_available"] is True
        assert data["gps_status"] == "GPS_STALE"
        assert data["location"]["latitude"] == 44.4268
        assert data["location"]["longitude"] == 26.1025
        assert data["location"]["captured_at"] == captured_at.isoformat()
        assert data["location"]["age_seconds"] >= 59
        assert data["location"]["is_stale"] is True


def test_alert_without_authorized_location_keeps_explicit_null_fallback() -> None:
    app = create_app(_settings())
    with TestClient(app) as client:
        activation = _activate(client, "activate-without-location")
        session_id = activation["session"]["id"]
        sos = client.post(
            f"/api/v1/sessions/{session_id}/sos",
            headers=_auth(activation["control_token"]),
            json={"event_id": "sos-without-location", "source": "web"},
        )
        assert sos.status_code == 200, sos.text

        events = asyncio.run(app.state.services.broker.recent(limit=20))
        notification = next(
            event for event in events if event.type == "alert.notification_requested"
        )
        assert notification.data["location"] is None
        assert notification.data["location_available"] is False
