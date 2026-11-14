from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from app.security import (
    ControlTokenService,
    ControlTokenSessionMismatch,
    ExpiredControlToken,
    InvalidControlToken,
)

SECRET = "a-test-secret-that-is-at-least-thirty-two-bytes-long"
NOW = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)


def test_issue_and_verify_returns_utc_claims() -> None:
    service = ControlTokenService(SECRET, nonce_factory=lambda: "nonce-1")

    token = service.issue("session-123", now=NOW, ttl=90)
    claims = service.verify(token, "session-123", now=NOW + timedelta(seconds=89))

    assert claims.session_id == "session-123"
    assert claims.token_id == "nonce-1"
    assert claims.issued_at == NOW
    assert claims.expires_at == NOW + timedelta(seconds=90)
    assert re.fullmatch(r"v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token)
    assert "=" not in token


def test_expiry_is_enforced_at_exact_boundary() -> None:
    service = ControlTokenService(SECRET)
    token = service.issue("session-123", now=NOW, ttl=timedelta(seconds=10))

    service.verify(token, "session-123", now=NOW + timedelta(seconds=9))
    with pytest.raises(ExpiredControlToken, match="expired"):
        service.verify(token, "session-123", now=NOW + timedelta(seconds=10))


def test_token_is_scoped_to_one_session() -> None:
    service = ControlTokenService(SECRET)
    token = service.issue("session-a", now=NOW)

    with pytest.raises(ControlTokenSessionMismatch):
        service.verify(token, "session-b", now=NOW)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda token: token + "x",
        lambda token: token.replace("v1.", "v2.", 1),
        lambda token: token.rsplit(".", 1)[0],
        lambda token: "not-a-token",
        lambda token: token.replace(".", ".=", 1),
    ],
)
def test_malformed_or_tampered_token_is_rejected(mutator) -> None:
    service = ControlTokenService(SECRET)
    token = service.issue("session-123", now=NOW)

    with pytest.raises(InvalidControlToken):
        service.verify(mutator(token), "session-123", now=NOW)


def test_signed_but_invalid_payload_is_rejected() -> None:
    invalid_payload = _base64url(json.dumps({"sid": "session-123"}).encode())
    signed_value = f"v1.{invalid_payload}"
    signature = _base64url(
        hmac.new(SECRET.encode(), signed_value.encode("ascii"), hashlib.sha256).digest()
    )
    token = f"{signed_value}.{signature}"

    with pytest.raises(InvalidControlToken, match="payload"):
        ControlTokenService(SECRET).verify(token, "session-123", now=NOW)


def test_service_secret_never_appears_in_repr_or_errors() -> None:
    service = ControlTokenService(SECRET)
    token = service.issue("session-123", now=NOW)

    assert SECRET not in repr(service)
    with pytest.raises(InvalidControlToken) as error:
        service.verify(token + "tampered", "session-123", now=NOW)
    assert SECRET not in str(error.value)


def test_timezone_awareness_and_configuration_are_validated() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        ControlTokenService("too-short")
    with pytest.raises(ValueError, match="positive"):
        ControlTokenService(SECRET, default_ttl=timedelta(0))

    service = ControlTokenService(SECRET)
    with pytest.raises(ValueError, match="timezone-aware"):
        service.issue("session-123", now=datetime(2026, 11, 14))
    with pytest.raises(ValueError, match="positive"):
        service.issue("session-123", now=NOW, ttl=0)


def test_tokens_receive_distinct_nonce_values() -> None:
    service = ControlTokenService(SECRET)
    first = service.issue("session-123", now=NOW)
    second = service.issue("session-123", now=NOW)

    assert first != second


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
