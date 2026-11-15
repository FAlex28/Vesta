"""v5: access without codes, GPS on the device, assistant in the earbuds, recording, false alarm."""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.assistant import AssistantAction, AssistantDecision, AssistantUnavailable
from app.config import Settings
from app.container import build_services
from app.main import create_app

SIGNING_SECRET = "test-control-signing-secret-with-more-than-thirty-two-bytes"
DEVICE_KEY = "test-device-key"
DASHBOARD_KEY = "test-dashboard-key"
DEVICE = "vesta-rpi-01"
WAV = base64.b64encode(b"RIFF" + b"\x00" * 60).decode()


def settings(tmp_path: Path, **overrides: object) -> Settings:
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
        "open_access": True,
        "database_path": str(tmp_path / "vesta.db"),
    }
    values.update(overrides)
    return Settings(**values)


class FakeGemini:
    def __init__(self) -> None:
        self.next = AssistantDecision("", "Nu am înțeles.", AssistantAction.NONE)
        self.calls: list[dict] = []
        self.fail = False

    async def decide(self, *, audio, mime_type, history, context):
        self.calls.append({"audio": audio, "mime_type": mime_type, "history": history, "context": context})
        if self.fail:
            raise AssistantUnavailable("down")
        return self.next


class FakeVoice:
    async def synthesize(self, text: str) -> tuple[bytes, str]:
        return b"ID3-fake-mp3:" + text.encode()[:20], "audio/mpeg"


@pytest.fixture
def gemini() -> FakeGemini:
    return FakeGemini()


def make_client(tmp_path: Path, gemini: FakeGemini | None = None, voice: bool = False, **overrides):
    resolved = settings(tmp_path, **overrides)
    services = build_services(resolved)
    services.push_sender = lambda subscription, payload: 201
    assert services.assistant is not None
    services.assistant.understanding = gemini
    services.assistant.speech = FakeVoice() if voice else None
    return TestClient(create_app(resolved, services=services)), services


@pytest.fixture
def client(tmp_path: Path, gemini: FakeGemini):
    test_client, _services = make_client(tmp_path, gemini, voice=True)
    with test_client:
        yield test_client


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


DASH = {"X-Dashboard-Key": DASHBOARD_KEY}
DEV = {"X-Device-Key": DEVICE_KEY}


def activate(client: TestClient, event_id: str = "act-1") -> dict:
    response = client.post(
        "/api/v1/device/events",
        headers=DEV,
        json={"event_id": event_id, "device_id": DEVICE, "event": "activate"},
    )
    assert response.status_code in {200, 201}, response.text
    return response.json()


def device_fix(client: TestClient, event_id: str, lat: float = 45.7489, lon: float = 21.2087, *, age: float = 0.0):
    captured = datetime.now(UTC) - timedelta(seconds=age)
    return client.post(
        "/api/v1/device/location",
        headers=DEV,
        json={
            "event_id": event_id,
            "device_id": DEVICE,
            "latitude": lat,
            "longitude": lon,
            "captured_at": captured.isoformat(),
            "accuracy_meters": 4.5,
            "satellites": 7,
        },
    )


def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached")


# ----------------------------------------------------------------------- open access


def test_open_access_gives_phone_and_contact_without_codes(client: TestClient) -> None:
    phone = client.post("/api/v1/access/open", json={"scope": "companion"})
    contact = client.post("/api/v1/access/open", json={"scope": "contact"})
    assert phone.status_code == 200 and contact.status_code == 200
    activation = activate(client)
    taken = client.get("/api/v1/companion/session", headers=auth(phone.json()["access_token"]))
    assert taken.json()["session"]["id"] == activation["session"]["id"]
    status = client.get("/api/v1/contact/status", headers=auth(contact.json()["access_token"]))
    assert status.status_code == 200
    # a contact grant is not a phone grant
    assert client.get("/api/v1/companion/session", headers=auth(contact.json()["access_token"])).status_code == 401


def test_open_access_is_off_by_default(tmp_path: Path) -> None:
    test_client, _ = make_client(tmp_path, open_access=False)
    with test_client:
        assert test_client.post("/api/v1/access/open", json={"scope": "companion"}).status_code == 404


def test_open_access_refused_outside_development(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import ConfigurationError

    monkeypatch.setenv("VESTA_ENVIRONMENT", "production")
    monkeypatch.setenv("VESTA_OPEN_ACCESS", "true")
    with pytest.raises(ConfigurationError):
        Settings.from_env()


# ----------------------------------------------------------------------- device GPS


def test_device_gps_joins_the_session_and_the_phone_updates_it(client: TestClient) -> None:
    assert device_fix(client, "fix-before").json() == {"session_id": None, "accepted_as_latest": False}
    activation = activate(client)
    session_id = activation["session"]["id"]
    token = activation["control_token"]
    client.post(f"/api/v1/sessions/{session_id}/sos", headers=auth(token), json={"event_id": "sos-1"})
    status = client.get("/api/v1/status", headers=DASH).json()
    [location] = status["alert_locations"]
    assert location["source"] == "device_gps" and location["latitude"] == 45.7489

    moved = device_fix(client, "fix-2", lat=45.75, lon=21.21)
    assert moved.json() == {"session_id": session_id, "accepted_as_latest": True}
    phone = client.post(
        f"/api/v1/sessions/{session_id}/location",
        headers=auth(token),
        json={
            "event_id": "phone-1",
            "latitude": 44.0,
            "longitude": 26.0,
            "captured_at": datetime.now(UTC).isoformat(),
            "source": "android",
        },
    )
    # The phone's GPS is the main source now: its newer fix is the one shown.
    assert phone.json()["accepted_as_latest"] is True
    latest = client.get("/api/v1/status", headers=DASH).json()["alert_locations"][0]
    assert latest["latitude"] == 44.0 and latest["source"] == "android"


def test_device_location_needs_the_device_key(client: TestClient) -> None:
    response = client.post(
        "/api/v1/device/location",
        json={"event_id": "x", "device_id": DEVICE, "latitude": 1, "longitude": 1,
              "captured_at": datetime.now(UTC).isoformat()},  # fmt: skip
    )
    assert response.status_code == 401


# ----------------------------------------------------------------------- assistant


def test_first_question_comes_right_after_activation(tmp_path: Path) -> None:
    test_client, services = make_client(tmp_path, first_checkin_delay_seconds=0.5)
    with test_client:
        activation = activate(test_client)
        session_id = activation["session"]["id"]
        assert activation["session"]["next_checkin_at"] is not None
        next_at = datetime.fromisoformat(activation["session"]["next_checkin_at"])
        created = datetime.fromisoformat(activation["session"]["created_at"])
        assert next_at - created == timedelta(seconds=0.5)
        prompt = test_client.post(
            f"/api/v1/sessions/{session_id}/assistant/prompt",
            headers=auth(activation["control_token"]),
            json={"event_id": "p1", "line": "checkin"},
        )
        assert prompt.status_code == 200
        body = prompt.json()
        assert "din greșeală" in body["text"]
        assert body["audio_base64"] is None  # no ElevenLabs key: the phone speaks with its own voice


def test_reply_false_alarm_stops_and_deletes_everything(client: TestClient, gemini: FakeGemini) -> None:
    activation = activate(client)
    session_id = activation["session"]["id"]
    token = activation["control_token"]
    device_fix(client, "fix-1")
    chunk = client.post(
        f"/api/v1/sessions/{session_id}/recording/chunks?seq=0",
        headers={**auth(token), "Content-Type": "audio/webm;codecs=opus"},
        content=b"\x1aE\xdf\xa3webm-header",
    )
    assert chunk.status_code == 200 and chunk.json() == {"chunks": 1, "size_bytes": 15}

    gemini.next = AssistantDecision("Da, a fost din greșeală.", "Bine.", AssistantAction.FALSE_ALARM)
    reply = client.post(
        f"/api/v1/sessions/{session_id}/assistant/reply",
        headers=auth(token),
        json={"event_id": "r1", "audio_base64": WAV, "mime_type": "audio/wav"},
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["action"] == "FALSE_ALARM" and body["applied"] is True
    assert body["session"]["state"] == "CANCELLED"
    assert body["session"]["cancellation_reason"] == "FALSE_ALARM"
    assert "alarmă falsă" in body["reply"]["text"]
    assert base64.b64decode(body["reply"]["audio_base64"]).startswith(b"ID3")
    assert gemini.calls[0]["mime_type"] == "audio/wav"
    assert "NU a fost anunțat" in gemini.calls[0]["context"]

    services = client.app.state.services
    wait_until(lambda: services.recordings.info(session_id) is None)
    assert services.assistant.turns(session_id) == []
    assert services.alerts.get_for_session(session_id) is None  # nobody was alerted
    # no more audio after a false alarm
    late = client.post(
        f"/api/v1/sessions/{session_id}/recording/chunks?seq=1",
        headers={**auth(token), "Content-Type": "audio/webm"},
        content=b"late",
    )
    assert late.status_code == 409


def test_reply_escalate_alerts_and_shares_voice_and_transcript(client: TestClient, gemini: FakeGemini) -> None:
    activation = activate(client)
    session_id = activation["session"]["id"]
    token = activation["control_token"]
    for seq, data in enumerate([b"\x1aE\xdf\xa3head", b"cluster-1", b"cluster-2"]):
        response = client.post(
            f"/api/v1/sessions/{session_id}/recording/chunks?seq={seq}",
            headers={**auth(token), "Content-Type": "audio/webm"},
            content=data,
        )
        assert response.status_code == 200
    # The dispatcher can follow the conversation while the session is live.
    before_alert = client.get(f"/api/v1/sessions/{session_id}/conversation", headers=DASH)
    assert before_alert.status_code == 200 and before_alert.json()["turns"] == []

    gemini.next = AssistantDecision("Nu, am nevoie de ajutor!", "Anunț dispeceratul.", AssistantAction.ESCALATE)
    reply = client.post(
        f"/api/v1/sessions/{session_id}/assistant/reply",
        headers=auth(token),
        json={"event_id": "r2", "audio_base64": WAV},
    )
    body = reply.json()
    assert body["applied"] is True and body["session"]["state"] == "ALERT"
    assert body["session"]["alert_reason"] == "SOS_EXPLICIT"

    conversation = client.get(f"/api/v1/sessions/{session_id}/conversation", headers=DASH)
    assert conversation.status_code == 200
    turns = conversation.json()["turns"]
    assert [t["role"] for t in turns] == ["person", "assistant"]
    assert turns[0]["text"] == "Nu, am nevoie de ajutor!" and turns[0]["action"] == "ESCALATE"
    assert conversation.json()["recording"]["chunks"] == 3

    # the dispatcher hears the recording from its first second (finished: session ended)
    client.post(
        f"/api/v1/sessions/{session_id}/alerts/{body['session']['alert']['id']}/resolve",
        headers=auth(token),
        json={"event_id": "res-1", "action": "USER_CONFIRMED_SAFE"},
    )
    services = client.app.state.services
    import asyncio

    asyncio.run(services.recordings.finalize(session_id))
    audio = client.get(f"/api/v1/sessions/{session_id}/recording", headers=DASH)
    assert audio.status_code == 200
    assert audio.content == b"\x1aE\xdf\xa3headcluster-1cluster-2"
    assert client.get(f"/api/v1/sessions/{session_id}/recording").status_code == 401


def test_reply_confirm_safe_answers_the_open_check_in(tmp_path: Path, gemini: FakeGemini) -> None:
    test_client, services = make_client(tmp_path, gemini, first_checkin_delay_seconds=0.1)
    with test_client:
        activation = activate(test_client)
        session_id = activation["session"]["id"]
        token = activation["control_token"]
        import asyncio

        asyncio.run(services.sessions.advance_all(now=datetime.now(UTC) + timedelta(seconds=1)))
        session = test_client.get(f"/api/v1/sessions/{session_id}", headers=auth(token)).json()
        assert session["state"] == "CHECK_IN"
        gemini.next = AssistantDecision("Sunt bine, mersi.", "Mă bucur.", AssistantAction.CONFIRM_SAFE)
        reply = test_client.post(
            f"/api/v1/sessions/{session_id}/assistant/reply",
            headers=auth(token),
            json={"event_id": "r3", "audio_base64": WAV},
        ).json()
        assert reply["applied"] is True and reply["session"]["state"] == "ACTIVE"


def test_reply_without_gemini_keeps_an_honest_transcript(tmp_path: Path) -> None:
    test_client, _services = make_client(tmp_path, None)
    with test_client:
        activation = activate(test_client)
        session_id = activation["session"]["id"]
        response = test_client.post(
            f"/api/v1/sessions/{session_id}/assistant/reply",
            headers=auth(activation["control_token"]),
            json={"event_id": "r4", "audio_base64": WAV},
        )
        assert response.status_code == 200
        assert response.json()["transcript"] == "Mesajul nu a putut fi transcris."
        assert response.json()["action"] == "NONE" and response.json()["applied"] is False
        assert "Nu am putut transcrie" in response.json()["reply"]["text"]
        status = test_client.get("/api/v1/status", headers=DASH).json()
        assert status["sessions"][0]["state"] == "ACTIVE"
        assert status["assistant"] == {"understanding": False, "voice": False}
        turns = test_client.get(f"/api/v1/sessions/{session_id}/conversation", headers=DASH).json()["turns"]
        assert [turn["role"] for turn in turns] == ["person", "assistant"]
        assert turns[0]["text"] == "Mesajul nu a putut fi transcris."


def test_unclear_false_alarm_without_words_is_not_applied(client: TestClient, gemini: FakeGemini) -> None:
    activation = activate(client)
    session_id = activation["session"]["id"]
    gemini.next = AssistantDecision("", "Nu am înțeles.", AssistantAction.FALSE_ALARM)
    reply = client.post(
        f"/api/v1/sessions/{session_id}/assistant/reply",
        headers=auth(activation["control_token"]),
        json={"event_id": "r5", "audio_base64": WAV},
    ).json()
    assert reply["applied"] is False and reply["session"]["state"] == "ACTIVE"


def test_false_alarm_button_also_stops_a_transmitted_alert(client: TestClient) -> None:
    activation = activate(client)
    session_id = activation["session"]["id"]
    token = activation["control_token"]
    device_fix(client, "fix-a")
    client.post(f"/api/v1/sessions/{session_id}/sos", headers=auth(token), json={"event_id": "sos-a"})
    cancel = client.post(
        f"/api/v1/sessions/{session_id}/cancel",
        headers=auth(token),
        json={"event_id": "fa-1", "reason": "FALSE_ALARM"},
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["session"]["state"] == "CANCELLED"
    assert cancel.json()["session"]["alert"]["resolution_action"] == "FALSE_ALARM"
    services = client.app.state.services
    wait_until(lambda: services.alerts.get_for_session(session_id) is not None)

    import asyncio

    def forgotten() -> bool:
        return asyncio.run(services.locations.get_last_authorized(session_id)) is None

    wait_until(forgotten)
    assert client.get(f"/api/v1/sessions/{session_id}/conversation", headers=DASH).status_code == 404


def test_recording_chunks_are_ordered(client: TestClient) -> None:
    activation = activate(client)
    session_id = activation["session"]["id"]
    headers = {**auth(activation["control_token"]), "Content-Type": "audio/webm"}
    url = f"/api/v1/sessions/{session_id}/recording/chunks"
    assert client.post(f"{url}?seq=1", headers=headers, content=b"x").status_code == 409
    assert client.post(f"{url}?seq=0", headers=headers, content=b"a").status_code == 200
    again = client.post(f"{url}?seq=0", headers=headers, content=b"a")
    assert again.json() == {"chunks": 1, "size_bytes": 1}  # a retry is not stored twice
    assert client.post(f"{url}?seq=1", headers=headers, content=b"").status_code == 422
    bad_type = client.post(f"{url}?seq=1", headers={**headers, "Content-Type": "text/plain"}, content=b"b")
    assert bad_type.status_code == 415
    assert client.post(f"{url}?seq=1", headers={"Content-Type": "audio/webm"}, content=b"b").status_code == 401


def test_button_simulator_activates_like_the_joystick(client: TestClient) -> None:
    first = client.post("/api/v1/simulator/press", json={"event_id": "sim-1"})
    assert first.status_code == 200
    assert first.json()["outcome"] == "ACTIVATED" and first.json()["state"] == "ACTIVE"
    again = client.post("/api/v1/simulator/press", json={"event_id": "sim-2"})
    assert again.json()["outcome"] == "ALREADY_ACTIVE"
    assert "control_token" not in first.json()
    status = client.get("/api/v1/status", headers=DASH).json()
    assert status["sessions"][0]["device_id"] == DEVICE


def test_button_simulator_is_off_without_open_access(tmp_path: Path) -> None:
    test_client, _ = make_client(tmp_path, open_access=False)
    with test_client:
        assert test_client.post("/api/v1/simulator/press", json={"event_id": "x"}).status_code == 404


def test_periodic_questions_rotate_and_reach_the_dispatcher(client: TestClient) -> None:
    activation = activate(client)
    session_id = activation["session"]["id"]
    token = activation["control_token"]

    def nudge(event_id: str) -> str:
        response = client.post(
            f"/api/v1/sessions/{session_id}/assistant/prompt",
            headers=auth(token),
            json={"event_id": event_id, "line": "nudge"},
        )
        assert response.status_code == 200, response.text
        return response.json()["text"]

    first, second = nudge("n1"), nudge("n2")
    assert first != second and nudge("n1") == first  # same event id, same line
    client.post(f"/api/v1/sessions/{session_id}/sos", headers=auth(token), json={"event_id": "sos-n"})
    assert "?" in nudge("n3")
    turns = client.get(f"/api/v1/sessions/{session_id}/conversation", headers=DASH).json()["turns"]
    assert [t["text"] for t in turns][:2] == [first, second]


@pytest.mark.parametrize("target", ["ACTIVE", "CHECK_IN", "UNCONFIRMED", "ALERT"])
def test_dispatcher_takes_over_any_ongoing_session_and_talks_to_her(
    tmp_path: Path, gemini: FakeGemini, target: str
) -> None:
    test_client, services = make_client(tmp_path, gemini, voice=True, first_checkin_delay_seconds=0.1)
    with test_client:
        activation = activate(test_client)
        session_id = activation["session"]["id"]
        token = activation["control_token"]
        url = f"/api/v1/sessions/{session_id}"
        if target in {"CHECK_IN", "UNCONFIRMED"}:
            import asyncio

            checkin_at = datetime.fromisoformat(activation["session"]["next_checkin_at"])
            asyncio.run(services.sessions.advance_all(now=checkin_at + timedelta(milliseconds=1)))
            if target == "UNCONFIRMED":
                checkin = test_client.get(url, headers=auth(token)).json()
                deadline = datetime.fromisoformat(checkin["response_deadline"])
                asyncio.run(services.sessions.advance_all(now=deadline + timedelta(milliseconds=1)))
        elif target == "ALERT":
            test_client.post(f"{url}/sos", headers=auth(token), json={"event_id": "sos-t"})

        assert test_client.get(url, headers=auth(token)).json()["state"] == target
        taken = test_client.post(f"{url}/takeover", headers=DASH)
        assert taken.status_code == 200, taken.text
        again = test_client.post(f"{url}/takeover", headers=DASH)
        assert again.json()["operator_connected_at"] == taken.json()["operator_connected_at"]
        session = test_client.get(url, headers=auth(token)).json()
        assert session["operator_connected_at"] == taken.json()["operator_connected_at"]

        # The assistant is silent now (except announcing the operator).
        nudge = test_client.post(f"{url}/assistant/prompt", headers=auth(token), json={"event_id": "nq", "line": "nudge"})
        assert nudge.status_code == 409
        assert nudge.json()["error"]["code"] in {"OPERATOR_CONNECTED", "NOT_NOW"}
        hand_over = test_client.post(f"{url}/assistant/prompt", headers=auth(token), json={"event_id": "op", "line": "operator"})
        assert "operator" in hand_over.json()["text"]
        reply = test_client.post(f"{url}/assistant/reply", headers=auth(token), json={"event_id": "rq", "audio_base64": WAV})
        assert reply.status_code == 409 and gemini.calls == []

        # Push to talk reaches the phone and is stored as a labeled transcript entry.
        sent = test_client.post(f"{url}/operator/messages", headers={**DASH, "Content-Type": "audio/webm"}, content=b"opus-1")
        assert sent.status_code == 200
        second = test_client.post(f"{url}/operator/messages", headers={**DASH, "Content-Type": "audio/webm"}, content=b"opus-2")
        messages = test_client.get(f"{url}/operator/messages", headers=auth(token)).json()
        assert [base64.b64decode(m["audio_base64"]) for m in messages] == [b"opus-1", b"opus-2"]
        newer = test_client.get(f"{url}/operator/messages?after={sent.json()['id']}", headers=auth(token)).json()
        assert [m["id"] for m in newer] == [second.json()["id"]]
        conversation = test_client.get(f"{url}/conversation", headers=DASH)
        assert conversation.status_code == 200
        assert conversation.json()["turns"][-1]["role"] == "operator"
        assert conversation.json()["turns"][-1]["text"] == "Mesaj vocal al dispecerului."
        assert test_client.get(f"{url}/operator/messages").status_code == 401
        assert test_client.post(f"{url}/operator/messages", content=b"x", headers={"Content-Type": "audio/webm"}).status_code == 401


def test_recordings_stay_in_the_database_after_a_restart(tmp_path: Path) -> None:
    test_client, services = make_client(tmp_path)
    with test_client:
        activation = activate(test_client)
        session_id = activation["session"]["id"]
        token = activation["control_token"]
        for seq, data in enumerate([b"webm-head", b"cluster"]):
            test_client.post(
                f"/api/v1/sessions/{session_id}/recording/chunks?seq={seq}",
                headers={**auth(token), "Content-Type": "audio/webm"},
                content=data,
            )
        test_client.post(f"/api/v1/sessions/{session_id}/assistant/prompt", headers=auth(token),
                         json={"event_id": "p", "line": "checkin"})  # fmt: skip
        test_client.post(f"/api/v1/sessions/{session_id}/sos", headers=auth(token), json={"event_id": "s"})
        assert test_client.post(f"/api/v1/sessions/{session_id}/takeover", headers=DASH).status_code == 200
        assert test_client.post(
            f"/api/v1/sessions/{session_id}/operator/messages",
            headers={**DASH, "Content-Type": "audio/webm"},
            content=b"operator-audio",
        ).status_code == 200
        wait_until(lambda: (services.recordings.record(session_id) or None) is not None
                   and services.recordings.record(session_id).alert_id is not None)  # fmt: skip

    # A new process on the same database: sessions are gone from memory, the archive is not.
    again, _ = make_client(tmp_path)
    with again:
        archive = again.get("/api/v1/recordings", headers=DASH).json()
        assert [item["session_id"] for item in archive] == [session_id]
        assert archive[0]["alert_reason"] == "SOS_EXPLICIT" and archive[0]["finalized"] is True
        assert archive[0]["turns"] == 2
        audio = again.get(f"/api/v1/sessions/{session_id}/recording", headers=DASH)
        assert audio.status_code == 200 and audio.content == b"webm-headcluster"
        conversation = again.get(f"/api/v1/sessions/{session_id}/conversation", headers=DASH).json()
        assert "din greșeală" in conversation["turns"][0]["text"]
        assert conversation["turns"][1]["role"] == "operator"
        assert conversation["turns"][1]["text"] == "Mesaj vocal al dispecerului."
        assert again.get("/api/v1/recordings").status_code == 401


class FakeTiger:
    """Stands in for TigerPersistence: records the mirrored writes, serves archived rows."""

    def __init__(self) -> None:
        self.chunks: list[tuple[str, int, bytes]] = []
        self.meta: dict[str, dict] = {}
        self.turns: list[tuple[str, str, str]] = []
        self.messages: list[str] = []
        self.purged: list[str] = []
        self.archived = {
            "session_id": "7b8a2c1e-0000-4000-8000-000000000001",
            "device_id": DEVICE,
            "started_at": datetime(2026, 11, 14, 9, 0, tzinfo=UTC),
            "updated_at": datetime(2026, 11, 14, 9, 1, tzinfo=UTC),
            "size_bytes": 9,
            "chunks": 1,
            "finalized": True,
            "alert_id": "old-alert",
            "alert_reason": "MISSED_CHECK_INS",
            "alert_at": datetime(2026, 11, 14, 9, 0, 30, tzinfo=UTC),
            "resolution": "USER_CONFIRMED_SAFE",
            "resolved_at": datetime(2026, 11, 14, 9, 1, tzinfo=UTC),
            "operator_at": None,
        }

    async def start(self):
        pass

    async def stop(self):
        pass

    # writes
    def recording_chunk(self, session_id, seq, data, mime_type, at):
        self.chunks.append((session_id, seq, data))

    def recording_meta(self, session_id, fields):
        self.meta.setdefault(session_id, {}).update(fields)

    def recording_operator_message(self, session_id, message):
        self.messages.append(session_id)

    def record_conversation_turn(self, session_id, turn):
        self.turns.append((session_id, turn.role, turn.text))

    def record_location(self, **kwargs):
        pass

    def purge_session_media(self, session_id):
        self.purged.append(session_id)

    def record_companion_heartbeat(self, **kwargs):
        pass

    def record_alert_viewed(self, alert_id):
        pass

    # reads
    async def recordings_archive(self):
        return [self.archived]

    async def recording_meta_read(self, session_id):
        return self.archived if session_id == self.archived["session_id"] else None

    async def recording_audio(self, session_id):
        return b"webm-tiger" if session_id == self.archived["session_id"] else b""

    async def conversation_turns(self, session_id):
        return [{"turn_id": "11111111-1111-4111-8111-111111111111", "occurred_at": datetime(2026, 11, 14, 9, 0, 31, tzinfo=UTC),
                 "role": "PERSON", "text": "Ajutor!", "action": "ESCALATE"}]  # fmt: skip


class FakeTranscriber(FakeGemini):
    async def transcribe(self, audio, mime_type):
        return "Rămâi pe loc, vine ajutorul."


def test_everything_is_mirrored_to_tiger_data_and_read_back(tmp_path: Path) -> None:
    tiger = FakeTiger()
    test_client, services = make_client(tmp_path, FakeTranscriber())
    services.persistence = tiger
    services.recordings.mirror = tiger
    original = services.assistant._recorder

    def keep(session_id, turn):
        original(session_id, turn)
        tiger.record_conversation_turn(session_id, turn)

    services.assistant._recorder = keep
    with test_client:
        activation = activate(test_client)
        session_id = activation["session"]["id"]
        token = activation["control_token"]
        test_client.post(
            f"/api/v1/sessions/{session_id}/recording/chunks?seq=0",
            headers={**auth(token), "Content-Type": "audio/webm"},
            content=b"webm-head",
        )
        test_client.post(f"/api/v1/sessions/{session_id}/sos", headers=auth(token), json={"event_id": "s-t"})
        assert test_client.post(f"/api/v1/sessions/{session_id}/takeover", headers=DASH).status_code == 200
        sent = test_client.post(
            f"/api/v1/sessions/{session_id}/operator/messages",
            headers={**DASH, "Content-Type": "audio/webm"},
            content=b"operator-voice",
        )
        assert sent.status_code == 200
        wait_until(lambda: tiger.meta.get(session_id, {}).get("alert_id") is not None)

        assert tiger.chunks == [(session_id, 0, b"webm-head")]
        assert tiger.meta[session_id]["alert_reason"] == "SOS_EXPLICIT"
        assert "operator_at" in tiger.meta[session_id]
        assert tiger.messages == [session_id]
        assert (session_id, "operator", "Rămâi pe loc, vine ajutorul.") in tiger.turns
        local = test_client.get(f"/api/v1/sessions/{session_id}/conversation", headers=DASH).json()["turns"]
        assert any(t["role"] == "operator" and t["text"] == "Rămâi pe loc, vine ajutorul." for t in local)

        # An alert known only to Tiger Data (another machine, local copy gone) is still there.
        archive = test_client.get("/api/v1/recordings", headers=DASH).json()
        old = tiger.archived["session_id"]
        assert old in [item["session_id"] for item in archive]
        audio = test_client.get(f"/api/v1/sessions/{old}/recording", headers=DASH)
        assert audio.status_code == 200 and audio.content == b"webm-tiger"
        turns = test_client.get(f"/api/v1/sessions/{old}/conversation", headers=DASH).json()["turns"]
        assert turns[0]["role"] == "person" and turns[0]["text"] == "Ajutor!"


def test_whisper_silence_and_subtitle_hallucinations_are_dropped() -> None:
    from app.assistant.service import _clean_transcript

    assert _clean_transcript({"text": "Să vă mulțumim pentru vizionare!"}) == ""
    assert _clean_transcript({"text": "x", "segments": [{"text": "x", "no_speech_prob": 0.9}]}) == ""
    real = {"text": "Ajutor", "segments": [{"text": "Ajutor, sunt pe Alba Iulia.", "no_speech_prob": 0.02}]}
    assert _clean_transcript(real) == "Ajutor, sunt pe Alba Iulia."



def test_live_call_offer_answer_and_hangup(client: TestClient) -> None:
    activation = activate(client)
    session_id = activation["session"]["id"]
    token = activation["control_token"]
    url = f"/api/v1/sessions/{session_id}"
    assert client.get(f"{url}/call", headers=auth(token)).json() == {"call_id": None, "offer": None, "answer": None}
    offer = client.post(f"{url}/call/offer", headers=DASH, json={"sdp": "v=0 offer-sdp"})
    assert offer.status_code == 200
    call_id = offer.json()["call_id"]
    # Calling her takes the call over: the assistant is quiet from now on.
    assert client.get(url, headers=auth(token)).json()["operator_connected_at"] is not None
    seen = client.get(f"{url}/call", headers=auth(token)).json()
    assert seen == {"call_id": call_id, "offer": "v=0 offer-sdp", "answer": None}
    assert client.get(f"{url}/call/answer", headers=DASH).json()["answer"] is None
    assert client.post(f"{url}/call/answer", headers=auth(token), json={"call_id": "other", "sdp": "v=0 answer"}).status_code == 409
    assert client.post(f"{url}/call/answer", headers=auth(token), json={"call_id": call_id, "sdp": "v=0 answer-sdp"}).status_code == 200
    assert client.get(f"{url}/call/answer", headers=DASH).json()["answer"] == "v=0 answer-sdp"
    assert client.get(f"{url}/call", headers=auth(token)).json()["offer"] is None  # already answered
    assert client.get(f"{url}/call").status_code == 401
    assert client.post(f"{url}/call/offer", json={"sdp": "v=0 offer-sdp"}).status_code == 401
    client.post(f"{url}/call/hangup", headers=DASH)
    assert client.get(f"{url}/call", headers=auth(token)).json()["call_id"] is None


def test_location_history_is_kept_for_an_alert_and_never_for_a_false_alarm(client: TestClient) -> None:
    activation = activate(client)
    session_id = activation["session"]["id"]
    token = activation["control_token"]
    url = f"/api/v1/sessions/{session_id}"

    def send(event_id: str, lat: float, lon: float, seconds_ago: int) -> None:
        client.post(
            f"{url}/location",
            headers=auth(token),
            json={
                "event_id": event_id,
                "latitude": lat,
                "longitude": lon,
                "accuracy_meters": 9,
                "captured_at": (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat(),
                "source": "android",
            },
        )

    send("p1", 45.7530, 21.2250, 20)
    # Before an alert the route is private, even for the dispatcher.
    assert client.get(f"{url}/locations", headers=DASH).status_code == 404
    client.post(f"{url}/sos", headers=auth(token), json={"event_id": "sos-h"})
    send("p2", 45.7536, 21.2257, 10)
    send("p3", 45.7541, 21.2263, 0)
    assert client.get(f"{url}/locations").status_code == 401
    history = client.get(f"{url}/locations", headers=DASH).json()
    assert [p["latitude"] for p in history["points"]] == [45.7530, 45.7536, 45.7541]
    assert history["points"][0]["accuracy_meters"] == 9 and history["points"][0]["source"] == "android"

    # A false alarm deletes the route too.
    client.post(f"{url}/cancel", headers=auth(token), json={"event_id": "fa-h", "reason": "FALSE_ALARM"})
    services = client.app.state.services
    wait_until(lambda: not services.recordings.locations(session_id))
    assert client.get(f"{url}/locations", headers=DASH).status_code == 404
