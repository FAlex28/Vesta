"""Phone pairing, trusted-contact access, screen-delivered check-ins and the served web app."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.container import build_services
from app.errors import AuthenticationError
from app.main import create_app
from app.safety import AudioStatus, SessionState
from app.services.access import AccessScope, AccessTokenService, PairingCodeRegistry
from app.voice import VoiceConnectionStatus

SIGNING_SECRET = "test-control-signing-secret-with-more-than-thirty-two-bytes"
DEVICE_KEY = "test-device-key"
DASHBOARD_KEY = "test-dashboard-key"
DEVICE = "vesta-rpi-01"
BASE_TIME = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "control_signing_secret": SIGNING_SECRET,
        "control_secret_is_ephemeral": False,
        "device_api_key": DEVICE_KEY,
        "dashboard_api_key": DASHBOARD_KEY,
        "voice_mode": "demo",
        "scheduler_enabled": False,
        "gpio_mode": "disabled",
        "gpio_device_id": DEVICE,
        "checkin_interval_seconds": 10.0,
        "checkin_response_timeout_seconds": 4.0,
        "checkin_retry_delay_seconds": 2.0,
        "checkin_max_attempts": 2,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app(settings())) as test_client:
        yield test_client


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def code_for(client: TestClient, scope: str) -> str:
    response = client.post(
        "/api/v1/pairing-codes",
        headers={"X-Dashboard-Key": DASHBOARD_KEY},
        json={"scope": scope},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["device_id"] == DEVICE and len(body["code"]) == 8
    return body["code"]


def claim(client: TestClient, code: str) -> dict[str, str]:
    response = client.post("/api/v1/pairing/claim", json={"code": code})
    assert response.status_code == 200, response.text
    return response.json()


def activate(client: TestClient, event_id: str = "activate-1", device_id: str = DEVICE) -> dict:
    response = client.post(
        "/api/v1/device/events",
        headers={"X-Device-Key": DEVICE_KEY},
        json={"event_id": event_id, "device_id": device_id, "event": "activate"},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- pairing


def test_pairing_codes_require_the_dashboard_key(client: TestClient) -> None:
    for headers in ({}, {"X-Dashboard-Key": "wrong"}):
        response = client.post("/api/v1/pairing-codes", headers=headers, json={"scope": "companion"})
        assert response.status_code == 401


def test_a_pairing_code_is_one_time_and_tolerates_spacing(client: TestClient) -> None:
    code = code_for(client, "companion")
    spaced = f"{code[:4].lower()} - {code[4:]}"
    granted = claim(client, spaced)
    assert granted["scope"] == "companion" and granted["device_id"] == DEVICE
    assert code not in granted["access_token"]
    again = client.post("/api/v1/pairing/claim", json={"code": code})
    assert again.status_code == 401


def test_invalid_codes_are_rate_limited() -> None:
    registry = PairingCodeRegistry(max_failures_per_minute=3)
    for _ in range(3):
        with pytest.raises(AuthenticationError):
            registry.claim("NOPE0000", now=BASE_TIME)
    with pytest.raises(Exception) as excinfo:
        registry.claim("NOPE0000", now=BASE_TIME)
    assert getattr(excinfo.value, "status_code", None) == 429
    # After a minute the window clears.
    code, _ = registry.issue(AccessScope.CONTACT, DEVICE, ttl=timedelta(minutes=5), now=BASE_TIME)
    assert registry.claim(code, now=BASE_TIME + timedelta(seconds=61)) == (AccessScope.CONTACT, DEVICE)


def test_expired_codes_and_tokens_are_refused() -> None:
    registry = PairingCodeRegistry()
    code, _ = registry.issue(AccessScope.COMPANION, DEVICE, ttl=timedelta(seconds=60), now=BASE_TIME)
    with pytest.raises(AuthenticationError):
        registry.claim(code, now=BASE_TIME + timedelta(seconds=61))
    tokens = AccessTokenService(SIGNING_SECRET)
    token, _ = tokens.issue(AccessScope.COMPANION, DEVICE, ttl=timedelta(hours=1), now=BASE_TIME)
    assert tokens.verify(token, AccessScope.COMPANION, now=BASE_TIME).device_id == DEVICE
    with pytest.raises(AuthenticationError):
        tokens.verify(token, AccessScope.COMPANION, now=BASE_TIME + timedelta(hours=2))


def test_scopes_and_token_kinds_never_substitute_for_each_other(client: TestClient) -> None:
    companion = claim(client, code_for(client, "companion"))["access_token"]
    contact = claim(client, code_for(client, "contact"))["access_token"]
    activation = activate(client)
    assert client.get("/api/v1/contact/status", headers=auth(companion)).status_code == 401
    assert client.get("/api/v1/companion/session", headers=auth(contact)).status_code == 401
    # A session control token is not a device grant, and a grant is not a control token.
    assert client.get("/api/v1/companion/session", headers=auth(activation["control_token"])).status_code == 401
    session_id = activation["session"]["id"]
    assert client.get(f"/api/v1/sessions/{session_id}", headers=auth(companion)).status_code == 401
    # A tampered token fails.
    tampered = companion[:-2] + ("AA" if not companion.endswith("AA") else "BB")
    assert client.get("/api/v1/companion/session", headers=auth(tampered)).status_code == 401


# --------------------------------------------------------------------------- companion


def test_paired_phone_receives_the_joystick_session_and_a_working_control_token(client: TestClient) -> None:
    companion = claim(client, code_for(client, "companion"))["access_token"]
    idle = client.get("/api/v1/companion/session", headers=auth(companion))
    assert idle.status_code == 200
    assert idle.json()["session"] is None and idle.json()["control_token"] is None

    activation = activate(client)  # stands in for the GPIO hold
    claimed = client.get("/api/v1/companion/session", headers=auth(companion)).json()
    assert claimed["session"]["id"] == activation["session"]["id"]
    beat = client.post(
        f"/api/v1/sessions/{claimed['session']['id']}/heartbeat",
        headers=auth(claimed["control_token"]),
        json={},
    )
    assert beat.status_code == 200


def test_a_phone_paired_with_another_device_gets_no_session(client: TestClient) -> None:
    response = client.post(
        "/api/v1/pairing-codes",
        headers={"X-Dashboard-Key": DASHBOARD_KEY},
        json={"scope": "companion", "device_id": "someone-else"},
    )
    other = claim(client, response.json()["code"])["access_token"]
    activate(client)
    claimed = client.get("/api/v1/companion/session", headers=auth(other)).json()
    assert claimed["session"] is None and claimed["control_token"] is None


# --------------------------------------------------------------------------- contact


def test_contact_sees_location_only_while_the_alert_is_active_and_records_viewing(client: TestClient) -> None:
    contact = claim(client, code_for(client, "contact"))["access_token"]
    assert client.get("/api/v1/contact/status", headers=auth(contact)).json()["alerts"] == []

    activation = activate(client)
    session_id = activation["session"]["id"]
    token = activation["control_token"]
    location = client.post(
        f"/api/v1/sessions/{session_id}/location",
        headers=auth(token),
        json={
            "event_id": "loc-1",
            "latitude": 45.75,
            "longitude": 21.22,
            "captured_at": datetime.now(UTC).isoformat(),
            "accuracy_meters": 12,
            "source": "android",
        },
    )
    assert location.status_code == 200
    assert client.post(
        f"/api/v1/sessions/{session_id}/sos", headers=auth(token), json={"event_id": "sos-1", "source": "android"}
    ).status_code == 200

    status = client.get("/api/v1/contact/status", headers=auth(contact)).json()
    [alert] = status["alerts"]
    assert alert["alert"]["status"] == "ACTIVE" and alert["alert_reason"] == "SOS_EXPLICIT"
    assert alert["location"]["latitude"] == 45.75 and alert["location_status"] == "CURRENT"
    assert alert["alert"]["viewed_at"] is None

    alert_id = alert["alert"]["id"]
    first = client.post(f"/api/v1/contact/alerts/{alert_id}/viewed", headers=auth(contact)).json()
    second = client.post(f"/api/v1/contact/alerts/{alert_id}/viewed", headers=auth(contact)).json()
    assert first["viewed_at"] is not None and first["viewed_at"] == second["viewed_at"]

    resolved = client.post(
        f"/api/v1/sessions/{session_id}/alerts/{alert_id}/resolve",
        headers=auth(token),
        json={"event_id": "resolve-1", "action": "USER_CONFIRMED_SAFE"},
    )
    assert resolved.status_code == 200
    [after] = client.get("/api/v1/contact/status", headers=auth(contact)).json()["alerts"]
    assert after["alert"]["status"] == "RESOLVED" and after["alert"]["resolved_at"]
    assert after["location"] is None and after["location_status"] == "UNAVAILABLE"
    assert after["alert"]["viewed_at"] == first["viewed_at"]


def test_contact_of_another_device_cannot_see_or_mark_the_alert(client: TestClient) -> None:
    response = client.post(
        "/api/v1/pairing-codes",
        headers={"X-Dashboard-Key": DASHBOARD_KEY},
        json={"scope": "contact", "device_id": "someone-else"},
    )
    stranger = claim(client, response.json()["code"])["access_token"]
    activation = activate(client)
    sos = client.post(
        f"/api/v1/sessions/{activation['session']['id']}/sos",
        headers=auth(activation["control_token"]),
        json={"event_id": "sos-1", "source": "android"},
    ).json()
    alert_id = sos["session"]["alert"]["id"]
    assert client.get("/api/v1/contact/status", headers=auth(stranger)).json()["alerts"] == []
    assert client.post(f"/api/v1/contact/alerts/{alert_id}/viewed", headers=auth(stranger)).status_code == 404


def test_status_has_no_power_reading_until_the_pi_reports_one(client: TestClient) -> None:
    body = client.get("/api/v1/status", headers={"X-Dashboard-Key": DASHBOARD_KEY}).json()
    assert body["power"] is None


# --------------------------------------------------------------------------- delivery


def _unanswered_twice(channel: str, *, audio_off: bool) -> SessionState:
    services = build_services(settings())

    async def scenario() -> SessionState:
        activation = await services.sessions.activate(event_id="a-1", device_id=DEVICE, now=BASE_TIME)
        session_id = activation.session.session_id
        token = activation.control_token

        async def hang_up(at: datetime, n: int) -> None:
            # The real path: the phone reports that the voice conversation ended.
            await services.sessions.set_voice_status(
                session_id, token=token, event_id=f"voice-off-{n}", status=VoiceConnectionStatus.DISCONNECTED, now=at
            )
            assert (await services.sessions.get(session_id)).audio_status is AudioStatus.DISCONNECTED

        if audio_off:
            await hang_up(BASE_TIME + timedelta(seconds=1), 0)
        clock = BASE_TIME + timedelta(seconds=10)
        for attempt in range(2):
            await services.sessions.advance_all(now=clock)
            current = await services.sessions.get(session_id)
            assert current.state is SessionState.CHECK_IN, (attempt, current.state)
            await services.sessions.mark_checkin_delivery(
                session_id,
                current.current_checkin_id,
                token=token,
                event_id=f"delivery-{attempt}",
                delivered=True,
                channel=channel,
                now=clock + timedelta(seconds=1),
            )
            if channel == "voice" and audio_off:
                # Voice delivery records audio as connected; the user then hangs up.
                await hang_up(clock + timedelta(seconds=2), attempt + 1)
            clock += timedelta(seconds=5)  # response timeout (4 s) passed
            await services.sessions.advance_all(now=clock)
            clock += timedelta(seconds=3)  # retry delay (2 s) passed
        return (await services.sessions.get(session_id)).state

    return asyncio.run(scenario())


def test_prompt_shown_on_the_phone_escalates_even_with_voice_off() -> None:
    assert _unanswered_twice("android", audio_off=True) is SessionState.ALERT


def test_voice_prompt_with_audio_lost_is_deferred_not_escalated() -> None:
    assert _unanswered_twice("voice", audio_off=True) is not SessionState.ALERT


def test_voice_prompt_with_audio_working_escalates() -> None:
    assert _unanswered_twice("voice", audio_off=False) is SessionState.ALERT


# --------------------------------------------------------------------------- web app


def test_backend_serves_the_built_web_app_on_every_route(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>Vesta</title>", encoding="utf-8")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    with TestClient(create_app(settings(frontend_dist=str(tmp_path)))) as test_client:
        for route in ("/", "/companion", "/contact"):
            page = test_client.get(route)
            assert page.status_code == 200 and "Vesta" in page.text
        assert test_client.get("/assets/app.js").text == "console.log(1)"
        assert test_client.get("/api/v1/nope").status_code == 404
        assert test_client.get("/../config.py").status_code in {200, 404}
        assert "SIGNING" not in test_client.get("/..%2Fconfig.py").text
        assert test_client.get("/health").json()["service"] == "vesta-backend"


def test_websocket_accepts_its_own_origin_and_rejects_foreign_ones(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect("/ws/events", headers={"origin": "http://testserver"}) as socket:
        socket.send_json({"type": "auth", "dashboard_key": DASHBOARD_KEY})
        assert socket.receive_json()["type"] == "system.snapshot"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/events", headers={"origin": "https://evil.example"}) as socket:
            socket.receive_json()
