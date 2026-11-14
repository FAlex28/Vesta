# Vesta API contract — backend, voice, GPIO and safety

Version: `v3` (UniHack 2026 prototype). v3 adds phone pairing, trusted-contact access,
check-in delivery semantics by channel, `viewed_at`, Pi power in status and the served web app.

This file defines only the interfaces owned by Agent 2. Vesta is a controlled demonstration,
not a validated emergency service. No endpoint calls 112 or infers that an assault occurred.

## Conventions and authorization

- Payloads are UTF-8 JSON, timestamps are ISO 8601 UTC and unknown fields are rejected.
- Mutating commands carry a unique `event_id` (maximum 128 characters). Replaying the same
  command is idempotent; reusing its ID for a different command returns `409 EVENT_ID_REUSED`.
- Session operations and reads require `Authorization: Bearer <control_token>`. The token is
  scoped to one session; knowing `session_id` alone is insufficient.
- Global dashboard reads require `X-Dashboard-Key` when `VESTA_DASHBOARD_API_KEY` is set.
- `/api/v1/device/events` is an integration/test boundary protected by `X-Device-Key` when
  configured. The production GPIO monitor calls the session service internally.
- Tokens and secrets must not appear in URLs, WebSocket events or logs. `ELEVENLABS_API_KEY`
  remains server-side.
- The in-memory idempotency registry retains the 10,000 most recent command IDs (LRU).
- With `VESTA_PERSISTENCE=tiger`, safety transitions are also queued asynchronously for Tiger
  Data. A database outage must not block check-ins, SOS, cancellation or alert resolution.

## Session and technical state

Safety state: `IDLE`, `ACTIVE`, `CHECK_IN`, `UNCONFIRMED`, `ALERT`, `CANCELLED`.

Technical health: `HEALTHY`, `DEGRADED`, `DISCONNECTED`. It is orthogonal to safety state.
Audio, GPS, power or connectivity loss cannot by itself become an emergency alert. Explicit
SOS always remains available.

Example session view (nullable fields may be omitted by older consumers):

```json
{
  "id": "5d9a9a55-1ca6-47bd-8768-43bcdb8492d4",
  "device_id": "vesta-rpi-01",
  "state": "ACTIVE",
  "health_status": "DEGRADED",
  "version": 1,
  "created_at": "2026-11-14T10:00:00Z",
  "updated_at": "2026-11-14T10:00:00Z",
  "quick_cancel_until": "2026-11-14T10:00:05Z",
  "checkin_id": null,
  "checkin_attempt": 0,
  "checkin_status": null,
  "missed_attempts": 0,
  "next_checkin_at": "2026-11-14T10:01:00Z",
  "response_deadline": null,
  "retry_at": null,
  "active_technical_issues": [],
  "page_visibility": "UNKNOWN",
  "connectivity_status": "UNKNOWN",
  "audio_status": "UNKNOWN",
  "gps_status": "GPS_UNAVAILABLE",
  "power_status": "POWER_STATUS_UNKNOWN",
  "alert_reason": null,
  "cancellation_reason": null,
  "alert": null
}
```

`version` increases on every effective state mutation and can be used by consumers to discard
stale updates.

## Physical activation

The joystick is wired directly to Raspberry Pi: `SW → BCM GPIO17` (physical pin 11) and
`GND → pin 6`. After a continuous hold of `VESTA_HOLD_SECONDS` (default 5 s), the monitor
queues exactly one internal activation event. A short press has no effect; a long hold never
repeats; release is required before another activation can be accepted. A low input present at
startup must first be released. Laptop/CI uses the deterministic `mock` adapter.

Only one unfinished safety session may exist. A different event during `ACTIVE`, `CHECK_IN`,
`UNCONFIRMED` or `ALERT` returns the current session with outcome `ALREADY_ACTIVE`; it is not
SOS, cancel or reset. A byte-identical retry of the original event has outcome `DUPLICATE`.

Integration route:

`POST /api/v1/device/events`

```json
{
  "event_id": "evt-001",
  "device_id": "vesta-rpi-01",
  "event": "activate"
}
```

```json
{
  "outcome": "ACTIVATED",
  "duplicate": false,
  "session": {},
  "control_token": "v1.<payload>.<signature>",
  "control_token_expires_at": "2026-11-14T14:00:00Z"
}
```

Outcomes are `ACTIVATED`, `DUPLICATE` or `ALREADY_ACTIVE`.

## Session endpoints

### Read and heartbeat

- `GET /api/v1/sessions/{id}` — scoped bearer token required; used for resynchronization.
- `POST /api/v1/sessions/{id}/heartbeat` with `{}` — renews the connectivity lease.

Lease expiry marks `DISCONNECTED`/`network_disconnected` but does not itself create `ALERT`.

### Quick or normal cancellation

`POST /api/v1/sessions/{id}/cancel`

```json
{
  "event_id": "cancel-001",
  "reason": "ACCIDENTAL_ACTIVATION"
}
```

`ACCIDENTAL_ACTIVATION` is valid only during the server-side quick-cancel window (default 5 s).
During that window, conversation and SOS are already usable. A transmitted alert cannot be
deleted through cancel; it must be resolved explicitly and retained in history.

### Safety check lifecycle

When the engine requests a check, it creates a unique `checkin_id`. Generation is not proof of
delivery.

`POST /api/v1/sessions/{id}/checkins/{checkin_id}/delivery`

```json
{
  "event_id": "delivery-001",
  "channel": "voice",
  "status": "DELIVERED"
}
```

`channel` is `voice`, `web` or `android`; `status` is `DELIVERED` or `UNDELIVERED`.

Only a check-in marked `DELIVERED` counts toward `ALERT` when it stays unanswered; an
undelivered one is retried. The companion page reports `android` when the prompt is shown on
the open (visible) page, or `voice` when the page is hidden but the voice conversation is
connected; it reports each `checkin_id` once. A prompt delivered by `voice` is deferred while
any audio/voice issue is active; a prompt delivered on screen (`web`/`android`) is deferred
only while the phone is disconnected, because the user saw it without audio.
The session view distinguishes the initial `REQUESTED` state from these outcomes. Audio
disconnect is recorded explicitly, and the web interface may still confirm.

`POST /api/v1/sessions/{id}/checkins`

```json
{
  "event_id": "checkin-001",
  "checkin_id": "chk-...",
  "confirmation": "safe",
  "source": "web"
}
```

`source` is `android`, `voice_agent`, `dashboard` or `web`. The backend validates the scoped
session, active `checkin_id`, allowed safety state and response deadline. A late confirmation
returns a conflict and cannot change or close an alert.

### Explicit SOS

`POST /api/v1/sessions/{id}/sos`

```json
{
  "event_id": "sos-001",
  "source": "android",
  "reason": "Am nevoie de ajutor"
}
```

The authorized command immediately transitions to `ALERT` with reason `SOS_EXPLICIT`, creates
or reuses the session's active alert, publishes dashboard state and requests one contact
notification. It works independently of ElevenLabs, GPS, power or connectivity.

### Resolve a transmitted alert

`POST /api/v1/sessions/{id}/alerts/{alert_id}/resolve`

```json
{
  "event_id": "resolve-001",
  "action": "USER_CONFIRMED_SAFE"
}
```

Resolution is explicit and authorized. The alert record is retained with `status: "RESOLVED"`,
`resolved_at` and `resolution_action`, and the outcome is published to dashboard/contact flows.
A late check-in never resolves an alert.

### Client conditions

`POST /api/v1/sessions/{id}/client-status`

```json
{
  "event_id": "client-001",
  "page_visibility": "HIDDEN",
  "connectivity_status": "DISCONNECTED",
  "audio_status": "DISCONNECTED",
  "gps_status": "GPS_STALE",
  "power_status": "LOW_VOLTAGE"
}
```

The backend accepts `GPS_AVAILABLE`, `GPS_STALE` and `GPS_UNAVAILABLE`, but location is not
required and coordinates are never fabricated. It accepts `POWER_UNSTABLE` and `LOW_VOLTAGE`,
but never invents a battery percentage. All timers stay server-side. Any subset of these flat
`*_status` fields may be submitted, but at least one field is required.

### Authorized last-known location

`POST /api/v1/sessions/{id}/location` requires the scoped bearer token.

```json
{
  "event_id": "location-001",
  "latitude": 44.4268,
  "longitude": 26.1025,
  "captured_at": "2026-11-14T10:02:00Z",
  "accuracy_meters": 18.5,
  "source": "android"
}
```

The response contains `duplicate`, `accepted_as_latest` and the accepted last-known location,
including `received_at`. Coordinates are validated, scoped to the authorized session and never
fabricated. An older but valid event is retained for idempotency without replacing a newer
location. A future timestamp outside the accepted skew returns `LOCATION_TIMESTAMP_INVALID`.

The compatibility route `POST /api/v1/sessions/{id}/technical-status` accepts an explicit
technical issue and `active` flag. Technical issues are history/status signals, not safety
conclusions.

## Phone pairing and trusted-contact access

Operators create one-time codes on the authenticated dashboard; the phone or the trusted
contact types the code once and receives a device-scoped bearer grant
(`g1.<payload>.<signature>`, HMAC with a key derived from `VESTA_CONTROL_SIGNING_SECRET`).
A grant never works as a control token, for another device or for the other role.

- `POST /api/v1/pairing-codes` (`X-Dashboard-Key`) `{"scope": "companion" | "contact",
  "device_id"?: "..."}` → `{scope, device_id, code, expires_at}`. 8 characters, one use,
  `VESTA_PAIRING_CODE_TTL_SECONDS` (600 s).
- `POST /api/v1/pairing/claim` `{"code": "ABCD2345"}` → `{scope, device_id, access_token,
  expires_at}`. Spaces/dashes and case are ignored. Wrong codes: `401`; more than 10 wrong
  codes per minute: `429 TOO_MANY_ATTEMPTS`.
- `GET /api/v1/companion/session` (Bearer companion grant) → `{device_id, session, control_token,
  control_token_expires_at}` for the device's unfinished session, or `session: null`. The phone
  polls this every 3 s, so a joystick activation is taken over without typing any token.
- `GET /api/v1/contact/status` (Bearer contact grant) → `{device_id, generated_at, alerts[]}`;
  each item has `session_id`, `state`, `alert_reason`, `alert`, `location_status`
  (`CURRENT`/`STALE`/`UNAVAILABLE`, stale after `VESTA_GPS_STALE_SECONDS`) and `location`
  (last authorized position with `age_seconds`, `is_stale`, `accuracy_meters`) **only while the
  alert is ACTIVE**.
- `POST /api/v1/contact/alerts/{alert_id}/viewed` (Bearer contact grant) → `AlertView`. Records
  the first display (`viewed_at`, event `alert.viewed`); later calls keep the first time. An
  alert of another device answers `404`.

## Alert model

Exactly one active alert exists per session. Concurrent SOS and automatic timeout converge on
the same `alert_id`; notification is requested once. An alert includes:

```json
{
  "id": "alert-...",
  "incident_type": "EXPLICIT_SOS",
  "status": "ACTIVE",
  "created_at": "2026-11-14T10:03:00Z",
  "updated_at": "2026-11-14T10:03:00Z",
  "notification_requested_at": "2026-11-14T10:03:00Z",
  "resolved_at": null,
  "resolution_action": null,
  "version": 1,
  "viewed_at": null
}
```

`viewed_at` is set only when the authorized contact page displayed the alert; creating the
alert or requesting its notification is never treated as seen.

When the alert is first transmitted, `alert.notification_requested` includes the last location
previously accepted through the authorized session endpoint, when one exists:

```json
{
  "alert_id": "alert-...",
  "gps_status": "GPS_STALE",
  "location_available": true,
  "location": {
    "latitude": 44.4268,
    "longitude": 26.1025,
    "captured_at": "2026-11-14T10:02:00Z",
    "age_seconds": 60.0,
    "is_stale": true,
    "accuracy_meters": 18.5,
    "source": "android"
  }
}
```

If no authorized location exists, `location` is `null` and `location_available` is `false`.
The alert is still transmitted; missing GPS never blocks SOS or timeout escalation.

## Voice

`POST /api/v1/sessions/{id}/voice/access`

```json
{
  "transport": "webrtc",
  "participant_name": "demo-user",
  "environment": "unihack-demo"
}
```

Live mode returns a short-lived `conversation_token`, never the permanent API key. Only one
conversation may be `CONNECTING`, `CONNECTED` or `RECONNECTING` for a session. Provider
failures use limited progressive backoff and mark the session degraded without blocking
SOS/cancel.

`POST /api/v1/sessions/{id}/voice/status` reports one of `CONNECTED`, `DISCONNECTED`,
`API_ERROR`, `MICROPHONE_ERROR`, `TIMEOUT`, `RECONNECTING`, `RECONNECTED` or
`SERVICE_UNAVAILABLE`, with the issued `conversation_id` where applicable. The payload also
contains a unique `event_id`. The backend does not store audio by default.

The language model cannot mutate safety state directly. Client tools only submit authorized,
validated commands for explicit confirmation or SOS.

## Recovery

At startup, the backend asks its recovery provider for unfinished sessions. Recovered
`ACTIVE`, `CHECK_IN`, `UNCONFIRMED` or `ALERT` records keep their identifiers, are marked as
process/audio interrupted and are republished. Restart never creates a replacement session and
never assumes the previous audio connection survived. A persistent adapter returns a
`RecoveredSessionBundle` containing the safety session, its existing alert record and persisted
idempotency records; this preserves `alert_id` and command replay behavior. More than one
unfinished session is rejected as an inconsistent GPIO deployment state. The persistence
provider is owned by the database/integration role; this repository defines only the hook and
safety behavior.

## Dashboard and events

`GET /api/v1/status` returns `system_state`, `system_health`, `generated_at`,
`latest_sequence`, `sessions`, `voice_mode` and `power` (`{state, detail, checked_at}` from
`vcgencmd get_throttled` on the Pi, or `null`; never a battery percentage). The first WebSocket
message, when a dashboard key is configured, must be `{"type":"auth","dashboard_key":...}` or
`{"type":"auth","session_id":...,"control_token":...}`. The WebSocket accepts origins in
`VESTA_CORS_ORIGINS` and pages served by the backend itself (`VESTA_FRONTEND_DIST`). `WS /ws/events` starts with an authenticated
snapshot and then ordered events. A slow client may receive `stream.resync_required` and must
refresh through an authorized read.

Important event types include:

- `session.activated`, `session.already_active`, `session.cancelled`, `session.recovered`
- `safety.check_in_requested`, `safety.check_in_delivered`, `safety.check_in_undelivered`,
  `safety.check_in_unconfirmed`, `safety.check_in_confirmed`, `safety.check_in_deferred`
- `safety.alerted`, `alert.notification_requested`, `safety.alert_resolved`, `alert.resolved`
- `technical.issue_reported`, `technical.issue_resolved`, `voice.status_changed`,
  `client.status_changed`, `technical.power`, `alert.viewed`
- `system.snapshot`, `system.heartbeat`, `stream.resync_required`

Each effective update includes the server-side session `version`. A client must not infer the
current safety state only from its local event history.

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

Expected conflict codes include `EVENT_ID_REUSED`, `INVALID_SESSION_TRANSITION`,
`QUICK_CANCEL_EXPIRED`, `CHECKIN_MISMATCH`,
`CHECKIN_EXPIRED` and `ALERT_ALREADY_RESOLVED`. Provider errors are sanitized.

## Persistence (Tiger Data)

Owned by Agent 3 (`backend/app/db`, `backend/app/integrations`; see `docs/database.md`).

- Enabled with `VESTA_PERSISTENCE=tiger` and `TIMESCALE_SERVICE_URL`. Default `memory` keeps the
  in-memory-only behaviour.
- `SessionManager` passes every non-`NO_CHANGE` transition to a recorder. Writes run in order in
  a background task, with deterministic event IDs derived from the client `event_id`, so a
  retried command is never stored twice.
- Alert closure still uses the explicit authorized resolve endpoint. Storage retains the alert
  as `RESOLVED`, including its timestamp/action, and queues one `ALERT_RESOLVED` notification
  for contacts that received the original alert.
- The default Tiger restart policy marks unfinished stored sessions `INTERRUPTED` in history,
  keeps stored active alerts open and publishes `system.recovered`; it does not invent a new live
  session. A deployment may replace that default startup policy with a persistent
  `SessionRecoveryProvider` that reconstructs one live `RecoveredSessionBundle`, preserving its
  `session_id`, `alert_id` and idempotency records; the two recovery policies must not run over
  the same stored session.
- Additional read-only WebSocket events:
  - `contact.notification`: `notification_id`, `alert_id`, `kind` (`ALERT_RAISED` /
    `ALERT_RESOLVED`), `recipient_id`, `incident_type`, `location_status`. It omits coordinates
    by data-minimization policy; the WebSocket itself remains authenticated as described above.
  - `technical.power`: `state` (`POWER_OK`, `LOW_VOLTAGE`, `POWER_UNSTABLE` or
    `POWER_STATUS_UNKNOWN`) and `detail`. No battery percentage is reported.
  - `system.recovered`
- Control tokens (`v1.<payload>.<signature>`) stay the only mechanism for session control. The
  `access_tokens` table is reserved for future read access by the trusted contact and the
  private dashboard (location, history) and is not used by these endpoints yet.

## Interfaces outside Agent 2

Storage, Tiger/TimescaleDB schema, frontend/Android implementation, location acquisition and
actual contact notification belong to other roles. Agent 2 receives their events or publishes
requests through this contract; it does not implement or alter those components.
