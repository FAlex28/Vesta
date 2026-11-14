# Vesta API contract - backend, voice and safety

Version: `v1` (UniHack 2026 prototype)

This file defines the interfaces owned by Person 2. Changes to the shared contract must be
agreed with the consumers in the frontend/Android, integration and firmware modules.

Vesta is a controlled demonstration, not a validated emergency system. No endpoint calls 112
or any other emergency service.

## Conventions

- HTTP payloads use UTF-8 JSON and reject unknown fields.
- Timestamps are ISO 8601 UTC values.
- `event_id` is a non-empty client-generated identifier, at most 128 characters.
- Repeating a retained `event_id` with the same command is idempotent and returns
  `"duplicate": true`. Reusing it for another payload returns `409 EVENT_ID_REUSED`. The
  in-memory MVP retains the 10,000 most recently used command IDs (LRU).
- Idempotency and sessions are held in memory by this backend. Persistence belongs to the
  database/integration module.
- Session control endpoints require `Authorization: Bearer <control_token>`.
- The control token is scoped to one session, expires, and must never be placed in a URL,
  WebSocket event, log or repository.
- If `VESTA_DEVICE_API_KEY` is configured, the ESP32 activation request also requires
  `X-Device-Key`.
- `GET /api/v1/status` and `/ws/events` are read-only demo interfaces without user
  authentication.
  They may be exposed only inside the controlled test environment; browser WebSockets enforce
  the configured Origin allowlist.

## Session states

`IDLE`, `ACTIVE`, `CHECK_IN`, `UNCONFIRMED`, `ALERT`, `CANCELLED`

Audio and connectivity failures are orthogonal technical issues. They do not change a session
to `ALERT`. Only the deterministic Python safety engine can transition to `ALERT`, either after
the configured unanswered attempts or after an explicit SOS.

The common session object is:

```json
{
  "id": "5d9a9a55-1ca6-47bd-8768-43bcdb8492d4",
  "device_id": "vesta-01",
  "state": "ACTIVE",
  "created_at": "2026-11-14T10:00:00Z",
  "updated_at": "2026-11-14T10:00:00Z",
  "checkin_attempt": 0,
  "missed_attempts": 0,
  "next_checkin_at": "2026-11-14T10:01:00Z",
  "response_deadline": null,
  "retry_at": null,
  "client_last_seen_at": null,
  "connection_lease_expires_at": null,
  "active_technical_issues": [],
  "alert_reason": null
}
```

`alert_reason` is either `missed_check_ins`, `explicit_sos` or `null`.

## HTTP endpoints

### Health

`GET /health`

Returns process health and the configured voice mode. It contains no secrets.

### Activate from ESP32

`POST /api/v1/device/events`

```json
{
  "event_id": "evt-001",
  "device_id": "vesta-01",
  "event": "activate"
}
```

The first accepted request returns `201`; an identical retry returns `200` and the same session.
A different activation while that device has an unfinished session returns
`409 SESSION_ALREADY_ACTIVE`.

```json
{
  "duplicate": false,
  "session": {},
  "control_token": "v1.<payload>.<signature>",
  "control_token_expires_at": "2026-11-14T14:00:00Z"
}
```

The Android client must keep the session token only in memory and send it as a bearer token for
the remaining session operations.

### System status

`GET /api/v1/status`

```json
{
  "system_state": "ACTIVE",
  "generated_at": "2026-11-14T10:00:02Z",
  "latest_sequence": 1,
  "sessions": [],
  "voice_mode": "elevenlabs"
}
```

The dashboard should call this endpoint after connecting or after a
`stream.resync_required` event.

### Confirm a safety check

`POST /api/v1/sessions/{id}/checkins`

```json
{
  "event_id": "checkin-001",
  "confirmation": "safe",
  "source": "android"
}
```

`source` is `android`, `voice_agent` or `dashboard`. A confirmation is accepted only during
`CHECK_IN` or `UNCONFIRMED`; it resets the unanswered-attempt counter and returns the session to
`ACTIVE`.

### Explicit SOS

`POST /api/v1/sessions/{id}/sos`

```json
{
  "event_id": "sos-001",
  "source": "android",
  "reason": "Am nevoie de ajutor"
}
```

This immediately and deterministically changes an active session to `ALERT`. It only emits the
demonstration alert event; it does not contact emergency services. The optional free-text
`reason` is not broadcast on the shared WebSocket stream.

### Authorized cancellation and demo reset

`POST /api/v1/sessions/{id}/cancel`

```json
{
  "event_id": "cancel-001",
  "reason": "Am ajuns în siguranță"
}
```

Cancellation requires the bearer token and is allowed from `ACTIVE`, `CHECK_IN`, `UNCONFIRMED`
or `ALERT`. It changes the session to `CANCELLED`. A new activation for the same device then
starts a fresh session, which is the supported reset flow for the next demonstration.

The check-in, SOS, cancellation and technical-status endpoints return:

```json
{
  "duplicate": false,
  "session": {}
}
```

### Renew the Android connectivity lease

`POST /api/v1/sessions/{id}/heartbeat`

Send an authenticated `{}` body periodically while the Android session is connected. The first
heartbeat enables connection monitoring for that session. Each heartbeat updates
`client_last_seen_at` and `connection_lease_expires_at`. If the lease expires, the backend marks
`network_disconnected` before evaluating check-in timeouts, so a real connection loss cannot by
itself become `ALERT`. A later heartbeat resolves that technical issue.

### Report an audio or network condition

`POST /api/v1/sessions/{id}/technical-status`

```json
{
  "event_id": "tech-001",
  "issue": "microphone_denied",
  "active": true,
  "source": "android"
}
```

`issue` is one of:

- `network_disconnected`
- `voice_unavailable`
- `microphone_denied`
- `audio_interrupted`

Send the same issue with `active: false` after recovery. While an issue is active, automatic
check-in escalation is deferred. Explicit SOS remains available.

### Request temporary ElevenLabs access

`POST /api/v1/sessions/{id}/voice/access`

```json
{
  "transport": "webrtc",
  "participant_name": "demo-user",
  "environment": "unihack-demo"
}
```

In live mode, the response contains a short-lived ElevenLabs `conversation_token` and
`conversation_id`. The permanent `ELEVENLABS_API_KEY` never leaves the backend. The Android
client connects directly to ElevenLabs over WebRTC; audio is not proxied through Raspberry Pi.

```json
{
  "provider": "elevenlabs",
  "mode": "live",
  "transport": "webrtc",
  "conversation_token": "temporary-token",
  "conversation_id": "conv_123",
  "client_config": {
    "language": "ro",
    "first_message": "...",
    "system_prompt": "...",
    "client_tools": [],
    "session_id": "5d9a9a55-1ca6-47bd-8768-43bcdb8492d4",
    "safety_event": "safety.check_in_requested"
  }
}
```

Offline demo mode returns `provider: "demo"`, `mode: "demo"`, `transport: "scripted"` and
scripted Romanian messages in `client_config.demo_messages`.

The client tools map only explicit user actions to the Vesta endpoints:

- `vesta_confirm_safe` -> `POST .../checkins`
- `vesta_request_help` -> `POST .../sos`
- `vesta_report_technical_issue` -> `POST .../technical-status`

The language model cannot write the safety state directly.

## WebSocket

`WS /ws/events`

The first message is `system.snapshot`; its `sequence` is an atomic watermark matching
`data.latest_sequence`. Subsequent messages use this envelope:

```json
{
  "event_id": "srv-...",
  "sequence": 12,
  "type": "safety.check_in_requested",
  "occurred_at": "2026-11-14T10:01:00Z",
  "session_id": "5d9a9a55-1ca6-47bd-8768-43bcdb8492d4",
  "device_id": "vesta-01",
  "data": {
    "from_state": "ACTIVE",
    "state": "CHECK_IN",
    "attempt": 1,
    "response_deadline": "2026-11-14T10:01:15+00:00"
  }
}
```

Published types:

- `session.activated`
- `safety.check_in_requested`
- `safety.check_in_unconfirmed`
- `safety.check_in_confirmed`
- `safety.check_in_deferred`
- `safety.alerted`
- `session.cancelled`
- `technical.issue_reported`
- `technical.issue_resolved`
- `system.snapshot`
- `system.heartbeat`
- `stream.resync_required`

A slow or reconnected client must refresh from `GET /api/v1/status`; it must not infer safety
state solely from its local WebSocket history.

`system.heartbeat` is a transport keepalive with `sequence: 0`; it does not advance the ordered
event watermark.

## Errors

Application errors use:

```json
{
  "error": {
    "code": "INVALID_SESSION_TRANSITION",
    "message": "...",
    "details": {}
  }
}
```

Expected codes include `UNAUTHORIZED`, `NOT_FOUND`, `EVENT_ID_REUSED`,
`SESSION_ALREADY_ACTIVE`, `INVALID_SESSION_TRANSITION`, `SESSION_CANCELLED` and sanitized
`VOICE_*` service errors. Validation errors use FastAPI's standard `422` body.

## Reserved interfaces owned by other roles

The proposal also names `POST /api/v1/telemetry`, `POST /api/v1/location` and
`GET /api/v1/sensors/history`. They are intentionally not implemented here: storage,
TimescaleDB/Tiger Data, location ingestion, ESP32 integration beyond activation and historical
queries belong to Person 3. Their schemas must be added here through an agreed contract before
implementation.
