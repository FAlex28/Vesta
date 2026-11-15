from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Raised when environment configuration is unsafe or inconsistent."""


def _get_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    value = default if raw is None else float(raw)
    if value <= minimum:
        raise ConfigurationError(f"{name} must be greater than {minimum}")
    return value


def _get_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    value = default if raw is None else int(raw)
    if value <= minimum:
        raise ConfigurationError(f"{name} must be greater than {minimum}")
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "Vesta Safety Backend"
    environment: str = "development"
    cors_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )

    checkin_interval_seconds: float = 60.0
    # First check-in after activation: the assistant asks "was it a mistake?" right away.
    first_checkin_delay_seconds: float | None = None
    checkin_response_timeout_seconds: float = 15.0
    checkin_retry_delay_seconds: float = 5.0
    checkin_max_attempts: int = 2
    quick_cancel_seconds: float = 5.0
    client_heartbeat_timeout_seconds: float = 45.0
    scheduler_tick_seconds: float = 0.25
    scheduler_enabled: bool = True

    gpio_mode: str = "mock"
    gpio_pin: int = 17
    gpio_hold_seconds: float = 5.0
    gpio_debounce_seconds: float = 0.08
    gpio_device_id: str = "vesta-rpi-01"
    gpio_lock_path: str = "/tmp/vesta-gpio17.lock"

    control_signing_secret: str = field(
        default_factory=lambda: secrets.token_urlsafe(48), repr=False
    )
    control_secret_is_ephemeral: bool = True
    control_token_ttl_seconds: int = 4 * 60 * 60
    device_api_key: str | None = field(default=None, repr=False)
    dashboard_api_key: str | None = field(default=None, repr=False)

    voice_mode: str = "elevenlabs"
    elevenlabs_api_key: str | None = field(default=None, repr=False)
    elevenlabs_agent_id: str | None = None
    elevenlabs_timeout_seconds: float = 5.0
    elevenlabs_retry_count: int = 1

    websocket_queue_size: int = 256
    websocket_heartbeat_seconds: float = 15.0
    websocket_auth_timeout_seconds: float = 5.0

    # "memory": doar în memorie (implicit, teste). "tiger": istoric în Tiger Data / TimescaleDB.
    persistence_mode: str = "memory"
    trusted_contact_ids: tuple[str, ...] = ("demo-contact",)

    # Phone pairing (code printed on the device) and emergency contacts (see app/services/access.py).
    device_pairing_code: str | None = field(default=None, repr=False)
    state_dir: str | None = None
    max_paired_phones: int = 5
    contact_invite_ttl_seconds: float = 24 * 60 * 60
    companion_token_ttl_seconds: int = 30 * 24 * 60 * 60
    contact_token_ttl_seconds: int = 30 * 24 * 60 * 60
    gps_stale_seconds: float = 60.0
    # Web Push for emergency contacts (generated and kept in the state dir when unset).
    vapid_private_key: str | None = field(default=None, repr=False)
    vapid_public_key: str | None = None
    vapid_subject: str = "mailto:vesta@example.invalid"
    # Stable public HTTPS address, used in the QR code and documentation only.
    public_url: str | None = None
    # Raspberry Pi camera service; credentials remain server-side and are never sent to browsers.
    camera_base_url: str | None = None
    camera_api_key: str | None = field(default=None, repr=False)
    # Built web app (frontend/dist) served by this process, so the phone needs one address.
    frontend_dist: str | None = None
    power_poll_seconds: float = 30.0
    # Hackathon/demo: the person's phone and the trusted contact open their pages without any
    # pairing code. Refused outside development/test.
    open_access: bool = False
    # Voice assistant: Gemini understands the person (audio in, decision out), ElevenLabs speaks.
    gemini_api_key: str | None = field(default=None, repr=False)
    gemini_model: str = "gemini-2.5-flash"
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"
    elevenlabs_tts_model: str = "eleven_flash_v2_5"
    assistant_timeout_seconds: float = 15.0
    # Local database (SQLite) with recordings, transcripts and the dispatcher's voice messages.
    # Default: VESTA_STATE_DIR/vesta.db; without a state dir it lives in memory (tests).
    database_path: str | None = None
    max_recording_bytes: int = 64 * 1024 * 1024

    @classmethod
    def from_env(cls) -> Settings:
        secret = (os.getenv("VESTA_CONTROL_SIGNING_SECRET") or "").strip() or None
        if secret is not None and len(secret.encode("utf-8")) < 32:
            raise ConfigurationError("VESTA_CONTROL_SIGNING_SECRET must contain at least 32 bytes")
        origins = tuple(
            origin.strip()
            for origin in os.getenv(
                "VESTA_CORS_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173",
            ).split(",")
            if origin.strip()
        )
        if not origins or "*" in origins:
            raise ConfigurationError("VESTA_CORS_ORIGINS must be an explicit, non-empty allowlist")

        voice_mode = os.getenv("VESTA_VOICE_MODE", "elevenlabs").strip().lower()
        if voice_mode not in {"elevenlabs", "demo"}:
            raise ConfigurationError("VESTA_VOICE_MODE must be 'elevenlabs' or 'demo'")

        gpio_mode = os.getenv("VESTA_GPIO_MODE", "mock").strip().lower()
        if gpio_mode not in {"disabled", "mock", "gpio"}:
            raise ConfigurationError("VESTA_GPIO_MODE must be 'disabled', 'mock' or 'gpio'")

        environment = os.getenv("VESTA_ENVIRONMENT", "development").strip().lower()
        device_api_key = (os.getenv("VESTA_DEVICE_API_KEY") or "").strip() or None
        dashboard_api_key = (os.getenv("VESTA_DASHBOARD_API_KEY") or "").strip() or None
        if environment not in {"development", "test"}:
            if secret is None:
                raise ConfigurationError(
                    "VESTA_CONTROL_SIGNING_SECRET is required outside development/test"
                )
            if device_api_key is None:
                raise ConfigurationError(
                    "VESTA_DEVICE_API_KEY is required outside development/test"
                )
            if dashboard_api_key is None:
                raise ConfigurationError(
                    "VESTA_DASHBOARD_API_KEY is required outside development/test"
                )
            if gpio_mode != "gpio":
                raise ConfigurationError("VESTA_GPIO_MODE must be 'gpio' outside development/test")

        persistence_mode = os.getenv("VESTA_PERSISTENCE", "memory").strip().lower()
        if persistence_mode not in {"memory", "tiger"}:
            raise ConfigurationError("VESTA_PERSISTENCE must be 'memory' or 'tiger'")
        contacts = tuple(
            c.strip()
            for c in os.getenv("VESTA_TRUSTED_CONTACT_IDS", "demo-contact").split(",")
            if c.strip()
        )

        retry_count = int(os.getenv("ELEVENLABS_RETRY_COUNT", "1"))
        if retry_count not in {0, 1}:
            raise ConfigurationError("ELEVENLABS_RETRY_COUNT must be 0 or 1")
        max_attempts = _get_int("VESTA_CHECKIN_MAX_ATTEMPTS", 2)
        if max_attempts < 2:
            raise ConfigurationError(
                "VESTA_CHECKIN_MAX_ATTEMPTS must be at least 2 to permit a retry"
            )

        gpio_device_id = os.getenv("VESTA_GPIO_DEVICE_ID", "vesta-rpi-01").strip()
        gpio_lock_path = os.getenv("VESTA_GPIO_LOCK_PATH", "/tmp/vesta-gpio17.lock").strip()
        if not gpio_device_id:
            raise ConfigurationError("VESTA_GPIO_DEVICE_ID must not be empty")
        if not gpio_lock_path:
            raise ConfigurationError("VESTA_GPIO_LOCK_PATH must not be empty")

        camera_base_url = (os.getenv("VESTA_CAMERA_BASE_URL") or "").strip().rstrip("/") or None
        camera_api_key = (os.getenv("VESTA_CAMERA_API_KEY") or "").strip() or None
        if camera_base_url:
            parsed_camera_url = urlsplit(camera_base_url)
            if parsed_camera_url.scheme not in {"http", "https"} or not parsed_camera_url.netloc:
                raise ConfigurationError("VESTA_CAMERA_BASE_URL must be an HTTP(S) URL")
            if parsed_camera_url.hostname not in {"127.0.0.1", "localhost", "::1"} and not camera_api_key:
                raise ConfigurationError("VESTA_CAMERA_API_KEY is required for a remote camera server")

        open_access = _get_bool("VESTA_OPEN_ACCESS", False)
        if open_access and environment not in {"development", "test"}:
            raise ConfigurationError("VESTA_OPEN_ACCESS is only allowed in development/test")
        first_delay_raw = (os.getenv("VESTA_FIRST_CHECKIN_DELAY_SECONDS") or "").strip()
        first_checkin_delay = float(first_delay_raw) if first_delay_raw else None
        if first_checkin_delay is not None and first_checkin_delay <= 0:
            raise ConfigurationError("VESTA_FIRST_CHECKIN_DELAY_SECONDS must be greater than 0")

        return cls(
            app_name=os.getenv("VESTA_APP_NAME", "Vesta Safety Backend"),
            environment=environment,
            cors_origins=origins,
            checkin_interval_seconds=_get_float("VESTA_CHECKIN_INTERVAL_SECONDS", 60.0),
            checkin_response_timeout_seconds=_get_float(
                "VESTA_CHECKIN_RESPONSE_TIMEOUT_SECONDS", 15.0
            ),
            checkin_retry_delay_seconds=_get_float("VESTA_CHECKIN_RETRY_DELAY_SECONDS", 5.0),
            checkin_max_attempts=max_attempts,
            quick_cancel_seconds=_get_float("VESTA_QUICK_CANCEL_SECONDS", 5.0),
            client_heartbeat_timeout_seconds=_get_float(
                "VESTA_CLIENT_HEARTBEAT_TIMEOUT_SECONDS", 45.0
            ),
            scheduler_tick_seconds=_get_float("VESTA_SCHEDULER_TICK_SECONDS", 0.25),
            scheduler_enabled=_get_bool("VESTA_SCHEDULER_ENABLED", True),
            gpio_mode=gpio_mode,
            gpio_pin=_get_int("VESTA_GPIO_PIN", 17, minimum=-1),
            gpio_hold_seconds=_get_float("VESTA_HOLD_SECONDS", 5.0),
            gpio_debounce_seconds=_get_float("VESTA_GPIO_DEBOUNCE_SECONDS", 0.08),
            gpio_device_id=gpio_device_id,
            gpio_lock_path=gpio_lock_path,
            control_signing_secret=secret or secrets.token_urlsafe(48),
            control_secret_is_ephemeral=secret is None,
            control_token_ttl_seconds=_get_int("VESTA_CONTROL_TOKEN_TTL_SECONDS", 14400),
            device_api_key=device_api_key,
            dashboard_api_key=dashboard_api_key,
            voice_mode=voice_mode,
            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY") or None,
            elevenlabs_agent_id=os.getenv("ELEVENLABS_AGENT_ID") or None,
            elevenlabs_timeout_seconds=_get_float("ELEVENLABS_TIMEOUT_SECONDS", 5.0),
            elevenlabs_retry_count=retry_count,
            websocket_queue_size=_get_int("VESTA_WEBSOCKET_QUEUE_SIZE", 256),
            websocket_heartbeat_seconds=_get_float("VESTA_WEBSOCKET_HEARTBEAT_SECONDS", 15.0),
            websocket_auth_timeout_seconds=_get_float("VESTA_WEBSOCKET_AUTH_TIMEOUT_SECONDS", 5.0),
            persistence_mode=persistence_mode,
            trusted_contact_ids=contacts,
            device_pairing_code=(os.getenv("VESTA_DEVICE_PAIRING_CODE") or "").strip() or None,
            state_dir=(os.getenv("VESTA_STATE_DIR") or "").strip() or None,
            max_paired_phones=_get_int("VESTA_MAX_PAIRED_PHONES", 5),
            contact_invite_ttl_seconds=_get_float("VESTA_CONTACT_INVITE_TTL_SECONDS", 86400.0),
            companion_token_ttl_seconds=_get_int("VESTA_COMPANION_TOKEN_TTL_SECONDS", 2592000),
            contact_token_ttl_seconds=_get_int("VESTA_CONTACT_TOKEN_TTL_SECONDS", 2592000),
            gps_stale_seconds=_get_float("VESTA_GPS_STALE_SECONDS", 60.0),
            vapid_private_key=(os.getenv("VESTA_VAPID_PRIVATE_KEY") or "").strip() or None,
            vapid_public_key=(os.getenv("VESTA_VAPID_PUBLIC_KEY") or "").strip() or None,
            vapid_subject=os.getenv("VESTA_VAPID_SUBJECT", "mailto:vesta@example.invalid").strip(),
            public_url=(os.getenv("VESTA_PUBLIC_URL") or "").strip().rstrip("/") or None,
            camera_base_url=camera_base_url,
            camera_api_key=camera_api_key,
            frontend_dist=(os.getenv("VESTA_FRONTEND_DIST") or "").strip() or None,
            power_poll_seconds=_get_float("VESTA_POWER_POLL_SECONDS", 30.0),
            first_checkin_delay_seconds=first_checkin_delay,
            open_access=open_access,
            gemini_api_key=(os.getenv("GEMINI_API_KEY") or "").strip() or None,
            gemini_model=(os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip(),
            elevenlabs_voice_id=(os.getenv("ELEVENLABS_VOICE_ID") or "JBFqnCBsd6RMkjVDRZzb").strip(),
            elevenlabs_tts_model=(os.getenv("ELEVENLABS_TTS_MODEL") or "eleven_flash_v2_5").strip(),
            assistant_timeout_seconds=_get_float("VESTA_ASSISTANT_TIMEOUT_SECONDS", 15.0),
            database_path=(os.getenv("VESTA_DATABASE_PATH") or "").strip() or None,
            max_recording_bytes=_get_int("VESTA_MAX_RECORDING_BYTES", 64 * 1024 * 1024),
        )
