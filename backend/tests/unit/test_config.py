from __future__ import annotations

import pytest

from app.config import ConfigurationError, Settings

ENV_NAMES = (
    "VESTA_ENVIRONMENT",
    "VESTA_CONTROL_SIGNING_SECRET",
    "VESTA_DEVICE_API_KEY",
    "VESTA_CORS_ORIGINS",
    "VESTA_VOICE_MODE",
    "ELEVENLABS_RETRY_COUNT",
    "VESTA_CHECKIN_MAX_ATTEMPTS",
    "VESTA_SCHEDULER_ENABLED",
)


@pytest.fixture(autouse=True)
def clean_vesta_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_development_defaults_use_explicit_origins_and_ephemeral_secret() -> None:
    settings = Settings.from_env()

    assert settings.environment == "development"
    assert settings.control_secret_is_ephemeral is True
    assert len(settings.control_signing_secret) >= 32
    assert "*" not in settings.cors_origins
    assert settings.checkin_max_attempts == 2


def test_non_development_configuration_fails_closed_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VESTA_ENVIRONMENT", "production")
    with pytest.raises(ConfigurationError, match="CONTROL_SIGNING_SECRET"):
        Settings.from_env()

    monkeypatch.setenv("VESTA_CONTROL_SIGNING_SECRET", "   ")
    with pytest.raises(ConfigurationError, match="CONTROL_SIGNING_SECRET"):
        Settings.from_env()

    monkeypatch.setenv("VESTA_CONTROL_SIGNING_SECRET", "x" * 48)
    with pytest.raises(ConfigurationError, match="DEVICE_API_KEY"):
        Settings.from_env()

    monkeypatch.setenv("VESTA_DEVICE_API_KEY", "device-secret")
    settings = Settings.from_env()
    assert settings.environment == "production"
    assert settings.control_secret_is_ephemeral is False


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("ELEVENLABS_RETRY_COUNT", "2", "RETRY_COUNT"),
        ("VESTA_CHECKIN_MAX_ATTEMPTS", "1", "MAX_ATTEMPTS"),
        ("VESTA_VOICE_MODE", "unknown", "VOICE_MODE"),
        ("VESTA_CORS_ORIGINS", "*", "CORS_ORIGINS"),
        ("VESTA_SCHEDULER_ENABLED", "sometimes", "boolean"),
    ],
)
def test_invalid_environment_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError, match=message):
        Settings.from_env()
