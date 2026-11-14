"""Phone pairing (device code), emergency contacts, push, delivery semantics, served web app."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.container import build_services
from app.errors import AuthenticationError
from app.main import create_app
from app.safety import AudioStatus, SessionState
from app.services.access import (
    AccessScope,
    AccessTokenService,
    AttemptLimiter,
    TooManyAttemptsError,
)
from app.services.directory import DeviceDirectory
from app.voice import VoiceConnectionStatus

SIGNING_SECRET = "test-control-signing-secret-with-more-than-thirty-two-bytes"
DEVICE_KEY = "test-device-key"
DASHBOARD_KEY = "test-dashboard-key"
DEVICE_CODE = "VESTA23456"
DEVICE = "vesta-rpi-01"
BASE_TIME = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "control_signing_secret": SIGNING_SECRET,
        "control_secret_is_ephemeral": False,
        "device_api_key": DEVICE_KEY,
        "dashboard_api_key": DASHBOARD_KEY,
        "device_pairing_code": DEVICE_CODE,
        "voice_mode": "demo",
        "scheduler_enabled": False,
        "gpio_mode": "disabled",
        "gpio_device_id": DEVICE,
        "checkin_interval_seconds": 10.0,
        "checkin_response_timeout_seconds": 4.0,
        "checkin_retry_delay_seconds": 2.0,
        "checkin_max_attempts": 2,
        "max_paired_phones": 2,
    }
    values.update(overrides)
    return Settings(**values)


class FakePush:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.status = 201

    def __call__(self, subscription: dict, payload: str) -> int:
        self.sent.append((subscription["endpoint"], json.loads(payload)))
        return self.status


@pytest.fixture
def push() -> FakePush:
    return FakePush()


@pytest.fixture
def client(push: FakePush) -> TestClient:
    services = build_services(settings())
    services.push_sender = push
    with TestClient(create_app(settings(), services=services)) as test_client:
        yield test_client


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def pair_phone(client: TestClient, code: str = DEVICE_CODE, label: str = "Pixel") -> str:
    response = client.post("/api/v1/pairing/device", json={"code": code, "label": label})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def invite_contact(client: TestClient, phone_token: str, name: str = "Mama", number: str = "+40 722 123 456") -> dict:
    response = client.post(
        "/api/v1/companion/contacts", headers=auth(phone_token), json={"name": name, "phone": number}
    )
    assert response.status_code == 200, response.text
    return response.json()


def claim_contact(client: TestClient, code: str) -> str:
    response = client.post("/api/v1/contact/claim", json={"code": code})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def activate(client: TestClient, event_id: str = "activate-1") -> dict:
    response = client.post(
        "/api/v1/device/events",
        headers={"X-Device-Key": DEVICE_KEY},
        json={"event_id": event_id, "device_id": DEVICE, "event": "activate"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def sos(client: TestClient, activation: dict) -> dict:
    response = client.post(
        f"/api/v1/sessions/{activation['session']['id']}/sos",
        headers=auth(activation["control_token"]),
        json={"event_id": "sos-1", "source": "android"},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- phone pairing


def test_the_dispatcher_no_longer_hands_out_codes(client: TestClient) -> None:
    response = client.post("/api/v1/pairing-codes", headers={"X-Dashboard-Key": DASHBOARD_KEY}, json={"scope": "companion"})
    assert response.status_code in {404, 405}


def test_phone_pairs_with_the_code_printed_on_the_device(client: TestClient) -> None:
    assert client.post("/api/v1/pairing/device", json={"code": "WRONG23456"}).status_code == 401
    token = pair_phone(client, code="vesta-23456")  # case and dashes do not matter
    profile = client.get("/api/v1/companion/profile", headers=auth(token)).json()
    assert profile["device_id"] == DEVICE and [p["label"] for p in profile["phones"]] == ["Pixel"]
    assert profile["this_phone_id"] == profile["phones"][0]["id"]


def test_the_device_code_is_reusable_but_phones_are_limited_and_revocable(client: TestClient) -> None:
    first = pair_phone(client, label="Telefon 1")
    second = pair_phone(client, label="Telefon 2")
    third = client.post("/api/v1/pairing/device", json={"code": DEVICE_CODE})
    assert third.status_code == 409 and third.json()["error"]["code"] == "TOO_MANY_PHONES"

    profile = client.get("/api/v1/companion/profile", headers=auth(first)).json()
    other = next(p for p in profile["phones"] if p["label"] == "Telefon 2")
    revoked = client.post(f"/api/v1/companion/phones/{other['id']}/revoke", headers=auth(first))
    assert [p["label"] for p in revoked.json()["phones"]] == ["Telefon 1"]
    assert client.get("/api/v1/companion/session", headers=auth(second)).status_code == 401
    assert client.get("/api/v1/companion/session", headers=auth(first)).status_code == 200


def test_wrong_codes_are_rate_limited() -> None:
    limiter = AttemptLimiter(max_failures_per_minute=3)
    for _ in range(3):
        limiter.check(BASE_TIME)
        limiter.failed(BASE_TIME)
    with pytest.raises(TooManyAttemptsError):
        limiter.check(BASE_TIME)
    limiter.check(BASE_TIME + timedelta(seconds=61))


def test_grants_expire_and_scopes_never_substitute(client: TestClient) -> None:
    tokens = AccessTokenService(SIGNING_SECRET)
    token, _expires, _jti = tokens.issue(AccessScope.COMPANION, DEVICE, ttl=timedelta(hours=1), now=BASE_TIME)
    assert tokens.verify(token, AccessScope.COMPANION, now=BASE_TIME).device_id == DEVICE
    with pytest.raises(AuthenticationError):
        tokens.verify(token, AccessScope.COMPANION, now=BASE_TIME + timedelta(hours=2))
    with pytest.raises(AuthenticationError):
        tokens.verify(token, AccessScope.CONTACT, now=BASE_TIME)

    phone = pair_phone(client)
    contact = claim_contact(client, invite_contact(client, phone)["code"])
    activation = activate(client)
    assert client.get("/api/v1/contact/status", headers=auth(phone)).status_code == 401
    assert client.get("/api/v1/companion/session", headers=auth(contact)).status_code == 401
    assert client.get("/api/v1/companion/session", headers=auth(activation["control_token"])).status_code == 401
    assert client.get(f"/api/v1/sessions/{activation['session']['id']}", headers=auth(phone)).status_code == 401


def test_paired_phone_receives_the_joystick_session_and_a_working_control_token(client: TestClient) -> None:
    phone = pair_phone(client)
    idle = client.get("/api/v1/companion/session", headers=auth(phone)).json()
    assert idle["session"] is None and idle["control_token"] is None
    activation = activate(client)
    claimed = client.get("/api/v1/companion/session", headers=auth(phone)).json()
    assert claimed["session"]["id"] == activation["session"]["id"]
    beat = client.post(
        f"/api/v1/sessions/{claimed['session']['id']}/heartbeat", headers=auth(claimed["control_token"]), json={}
    )
    assert beat.status_code == 200


def test_pairing_survives_a_restart_with_a_state_dir(tmp_path: Path) -> None:
    def make() -> TestClient:
        return TestClient(create_app(settings(state_dir=str(tmp_path), device_pairing_code=None)))

    with make() as first:
        code = DeviceDirectory(tmp_path / "directory.json").device_code(None)
        token = pair_phone(first, code=code)
        invite_contact(first, token)
    with make() as second:
        profile = second.get("/api/v1/companion/profile", headers=auth(token))
        assert profile.status_code == 200 and profile.json()["contacts"][0]["name"] == "Mama"
    stored = (tmp_path / "directory.json").read_text(encoding="utf-8")
    assert token not in stored


# --------------------------------------------------------------------------- emergency contacts


def test_the_phone_invites_a_contact_who_claims_once(client: TestClient) -> None:
    phone = pair_phone(client)
    client.post("/api/v1/companion/profile", headers=auth(phone), json={"display_name": "Ana"})
    invite = invite_contact(client, phone)
    assert invite["contact"]["phone"] == "+40722123456" and invite["contact"]["invite_pending"]
    assert len(invite["code"]) == 8

    contact = claim_contact(client, invite["code"].lower())
    assert client.post("/api/v1/contact/claim", json={"code": invite["code"]}).status_code == 401
    status = client.get("/api/v1/contact/status", headers=auth(contact)).json()
    assert status["person_name"] == "Ana" and status["contact_name"] == "Mama" and status["alerts"] == []
    accepted = client.get("/api/v1/companion/profile", headers=auth(phone)).json()["contacts"][0]
    assert accepted["accepted_at"] and not accepted["invite_pending"]


def test_invalid_contacts_and_duplicates_are_refused(client: TestClient) -> None:
    phone = pair_phone(client)
    bad = client.post("/api/v1/companion/contacts", headers=auth(phone), json={"name": "X", "phone": "abc123"})
    assert bad.status_code == 422
    invite_contact(client, phone)
    again = client.post("/api/v1/companion/contacts", headers=auth(phone), json={"name": "Alt", "phone": "+40722123456"})
    assert again.status_code == 409


def test_revoking_a_contact_cuts_its_access_and_its_notifications(client: TestClient) -> None:
    phone = pair_phone(client)
    invite = invite_contact(client, phone)
    contact = claim_contact(client, invite["code"])
    revoke = client.post(f"/api/v1/companion/contacts/{invite['contact']['id']}/revoke", headers=auth(phone))
    assert revoke.status_code == 200 and revoke.json()["contacts"] == []
    assert client.get("/api/v1/contact/status", headers=auth(contact)).status_code == 401


def test_contact_sees_location_only_while_the_alert_is_active_and_records_viewing(client: TestClient) -> None:
    phone = pair_phone(client)
    contact = claim_contact(client, invite_contact(client, phone)["code"])
    activation = activate(client)
    session_id = activation["session"]["id"]
    token = activation["control_token"]
    assert client.post(
        f"/api/v1/sessions/{session_id}/location",
        headers=auth(token),
        json={"event_id": "loc-1", "latitude": 45.75, "longitude": 21.22, "captured_at": datetime.now(UTC).isoformat(), "accuracy_meters": 12, "source": "android"},
    ).status_code == 200
    alert_id = sos(client, activation)["session"]["alert"]["id"]

    [alert] = client.get("/api/v1/contact/status", headers=auth(contact)).json()["alerts"]
    assert alert["alert"]["status"] == "ACTIVE" and alert["location"]["latitude"] == 45.75
    first = client.post(f"/api/v1/contact/alerts/{alert_id}/viewed", headers=auth(contact)).json()
    second = client.post(f"/api/v1/contact/alerts/{alert_id}/viewed", headers=auth(contact)).json()
    assert first["viewed_at"] and first["viewed_at"] == second["viewed_at"]

    assert client.post(
        f"/api/v1/sessions/{session_id}/alerts/{alert_id}/resolve",
        headers=auth(token),
        json={"event_id": "resolve-1", "action": "USER_CONFIRMED_SAFE"},
    ).status_code == 200
    [after] = client.get("/api/v1/contact/status", headers=auth(contact)).json()["alerts"]
    assert after["alert"]["status"] == "RESOLVED" and after["location"] is None


# --------------------------------------------------------------------------- dispatcher


def test_dispatcher_sees_the_location_only_during_an_active_alert(client: TestClient) -> None:
    activation = activate(client)
    session_id = activation["session"]["id"]
    token = activation["control_token"]
    client.post(
        f"/api/v1/sessions/{session_id}/location",
        headers=auth(token),
        json={"event_id": "loc-1", "latitude": 45.75, "longitude": 21.22, "captured_at": datetime.now(UTC).isoformat(), "source": "android"},
    )
    status = lambda: client.get("/api/v1/status", headers={"X-Dashboard-Key": DASHBOARD_KEY}).json()  # noqa: E731
    assert status()["alert_locations"] == []
    alert_id = sos(client, activation)["session"]["alert"]["id"]
    [location] = status()["alert_locations"]
    assert location["session_id"] == session_id and location["latitude"] == 45.75
    client.post(
        f"/api/v1/sessions/{session_id}/alerts/{alert_id}/resolve",
        headers=auth(token),
        json={"event_id": "resolve-1", "action": "USER_CONFIRMED_SAFE"},
    )
    assert status()["alert_locations"] == []


def test_status_has_no_power_reading_until_the_pi_reports_one(client: TestClient) -> None:
    assert client.get("/api/v1/status", headers={"X-Dashboard-Key": DASHBOARD_KEY}).json()["power"] is None


# --------------------------------------------------------------------------- push


def _wait_for(predicate, timeout: float = 3.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def test_alert_and_resolution_are_pushed_once_without_coordinates(client: TestClient, push: FakePush) -> None:
    phone = pair_phone(client)
    client.post("/api/v1/companion/profile", headers=auth(phone), json={"display_name": "Ana"})
    contact = claim_contact(client, invite_contact(client, phone)["code"])
    assert len(client.get("/api/v1/contact/push/key", headers=auth(contact)).json()["public_key"]) > 60
    subscription = {"endpoint": "https://push.example.test/abc", "keys": {"p256dh": "B" * 87, "auth": "A" * 22}}
    assert client.post("/api/v1/contact/push/subscribe", headers=auth(contact), json=subscription).status_code == 204
    assert client.post(
        "/api/v1/contact/push/subscribe", headers=auth(contact), json={**subscription, "endpoint": "http://insecure.test/x"}
    ).status_code == 401

    activation = activate(client)
    client.post(
        f"/api/v1/sessions/{activation['session']['id']}/location",
        headers=auth(activation["control_token"]),
        json={"event_id": "loc-1", "latitude": 45.75, "longitude": 21.22, "captured_at": datetime.now(UTC).isoformat(), "source": "android"},
    )
    alert_id = sos(client, activation)["session"]["alert"]["id"]
    _wait_for(lambda: len(push.sent) == 1)
    endpoint, payload = push.sent[0]
    assert endpoint == subscription["endpoint"] and payload["kind"] == "ALERT_RAISED"
    assert "Ana" in payload["body"] and "45.75" not in json.dumps(payload)
    status = client.get("/api/v1/contact/status", headers=auth(contact)).json()
    assert status["push_enabled"] and status["alerts"][0]["push_status"] == "SENT"

    client.post(
        f"/api/v1/sessions/{activation['session']['id']}/alerts/{alert_id}/resolve",
        headers=auth(activation["control_token"]),
        json={"event_id": "resolve-1", "action": "USER_CONFIRMED_SAFE"},
    )
    _wait_for(lambda: len(push.sent) == 2)
    assert push.sent[1][1]["kind"] == "ALERT_RESOLVED"


def test_a_gone_push_subscription_is_removed(client: TestClient, push: FakePush) -> None:
    push.status = 410
    phone = pair_phone(client)
    contact = claim_contact(client, invite_contact(client, phone)["code"])
    client.post(
        "/api/v1/contact/push/subscribe",
        headers=auth(contact),
        json={"endpoint": "https://push.example.test/gone", "keys": {"p256dh": "B" * 87, "auth": "A" * 22}},
    )
    sos(client, activate(client))
    _wait_for(lambda: len(push.sent) == 1)
    _wait_for(lambda: not client.get("/api/v1/contact/status", headers=auth(contact)).json()["push_enabled"])


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
