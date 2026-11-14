"""Short-lived, session-scoped control tokens for Vesta.

The token format intentionally stays small and dependency-free::

    v1.<base64url-json-payload>.<base64url-hmac-sha256>

Only the server knows the signing secret.  Tokens authorize control actions for
one session and must never be treated as user identity tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

_TOKEN_VERSION = "v1"
_PAYLOAD_VERSION = 1
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_PAYLOAD_LENGTH = 2_048


class ControlTokenError(ValueError):
    """Base class for failures while validating a control token."""


class InvalidControlToken(ControlTokenError):
    """The token is malformed or its signature is invalid."""


class ExpiredControlToken(ControlTokenError):
    """The token is valid but no longer active."""


class ControlTokenSessionMismatch(ControlTokenError):
    """The token was issued for a different session."""


@dataclass(frozen=True, slots=True)
class ControlTokenClaims:
    """Validated claims returned to the API authorization layer."""

    session_id: str
    issued_at: datetime
    expires_at: datetime
    token_id: str


class ControlTokenService:
    """Issue and validate HMAC-signed control tokens.

    ``secret`` must be a high-entropy server-side value.  It is deliberately
    omitted from ``repr`` and from every validation error.
    """

    __slots__ = ("_default_ttl", "_nonce_factory", "_secret")

    def __init__(
        self,
        secret: str | bytes,
        *,
        default_ttl: timedelta = timedelta(hours=2),
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
        if not isinstance(secret_bytes, bytes) or len(secret_bytes) < 32:
            raise ValueError("control-token signing secret must contain at least 32 bytes")
        if default_ttl <= timedelta(0):
            raise ValueError("default token lifetime must be positive")

        self._secret = secret_bytes
        self._default_ttl = default_ttl
        self._nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(18))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(default_ttl={self._default_ttl!r})"

    def issue(
        self,
        session_id: str,
        *,
        now: datetime | None = None,
        ttl: timedelta | int | float | None = None,
    ) -> str:
        """Return a new URL-safe token scoped to ``session_id``.

        Numeric ``ttl`` values are interpreted as seconds.  Explicit times must
        be timezone-aware so tokens cannot silently depend on the host timezone.
        """

        validated_session_id = _validate_session_id(session_id)
        issued_at = _utc_now(now)
        lifetime = _coerce_ttl(ttl, self._default_ttl)
        expires_at = issued_at + lifetime
        issued_timestamp = int(issued_at.timestamp())
        expires_timestamp = int(expires_at.timestamp())
        if expires_timestamp <= issued_timestamp:
            raise ValueError("token lifetime is too short")

        token_id = self._nonce_factory()
        if not isinstance(token_id, str) or not token_id or len(token_id) > 128:
            raise ValueError("nonce factory returned an invalid token id")

        payload = {
            "exp": expires_timestamp,
            "iat": issued_timestamp,
            "jti": token_id,
            "sid": validated_session_id,
            "v": _PAYLOAD_VERSION,
        }
        encoded_payload = _base64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signed_value = f"{_TOKEN_VERSION}.{encoded_payload}"
        signature = self._signature(signed_value)
        return f"{signed_value}.{signature}"

    def verify(
        self,
        token: str,
        expected_session_id: str,
        *,
        now: datetime | None = None,
    ) -> ControlTokenClaims:
        """Validate ``token`` and return its typed claims.

        Signature verification happens before parsing untrusted JSON.  Session
        mismatch and expiry use distinct exception types so the API layer can
        map them to stable error codes without disclosing token contents.
        """

        expected_session = _validate_session_id(expected_session_id)
        checked_at = _utc_now(now)

        if not isinstance(token, str) or len(token) > 4_096:
            raise InvalidControlToken("invalid control token")
        parts = token.split(".")
        if len(parts) != 3:
            raise InvalidControlToken("invalid control token")
        version, encoded_payload, provided_signature = parts
        if (
            version != _TOKEN_VERSION
            or not encoded_payload
            or len(encoded_payload) > _MAX_PAYLOAD_LENGTH
            or not _is_base64url(encoded_payload)
            or not _is_base64url(provided_signature)
        ):
            raise InvalidControlToken("invalid control token")

        signed_value = f"{version}.{encoded_payload}"
        expected_signature = self._signature(signed_value)
        if not hmac.compare_digest(expected_signature, provided_signature):
            raise InvalidControlToken("invalid control token")

        payload = _decode_payload(encoded_payload)
        session_id, issued_at, expires_at, token_id = _validate_payload(payload)

        if not hmac.compare_digest(session_id, expected_session):
            raise ControlTokenSessionMismatch("control token is not valid for this session")
        if checked_at >= expires_at:
            raise ExpiredControlToken("control token has expired")

        return ControlTokenClaims(
            session_id=session_id,
            issued_at=issued_at,
            expires_at=expires_at,
            token_id=token_id,
        )

    def _signature(self, signed_value: str) -> str:
        digest = hmac.new(
            self._secret,
            signed_value.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return _base64url_encode(digest)


def _utc_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(UTC)


def _coerce_ttl(
    value: timedelta | int | float | None,
    default: timedelta,
) -> timedelta:
    if value is None:
        lifetime = default
    elif isinstance(value, bool):
        raise ValueError("token lifetime must be positive")
    elif isinstance(value, (int, float)):
        lifetime = timedelta(seconds=value)
    elif isinstance(value, timedelta):
        lifetime = value
    else:
        raise TypeError("ttl must be a timedelta or a number of seconds")
    if lifetime <= timedelta(0):
        raise ValueError("token lifetime must be positive")
    return lifetime


def _validate_session_id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("session_id must be a non-empty string of at most 256 characters")
    return value


def _is_base64url(value: str) -> bool:
    return bool(value) and _BASE64URL_RE.fullmatch(value) is not None


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not _is_base64url(value):
        raise InvalidControlToken("invalid control token")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise InvalidControlToken("invalid control token") from exc
    if _base64url_encode(decoded) != value:
        raise InvalidControlToken("invalid control token")
    return decoded


def _decode_payload(encoded_payload: str) -> dict[str, Any]:
    raw_payload = _base64url_decode(encoded_payload)
    try:
        decoded = json.loads(raw_payload, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InvalidControlToken("invalid control token payload") from exc
    if not isinstance(decoded, dict):
        raise InvalidControlToken("invalid control token payload")
    return decoded


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate token claim")
        result[key] = value
    return result


def _validate_payload(
    payload: dict[str, Any],
) -> tuple[str, datetime, datetime, str]:
    if set(payload) != {"exp", "iat", "jti", "sid", "v"}:
        raise InvalidControlToken("invalid control token payload")
    if payload["v"] != _PAYLOAD_VERSION or isinstance(payload["v"], bool):
        raise InvalidControlToken("invalid control token payload")

    session_id = payload["sid"]
    token_id = payload["jti"]
    issued_timestamp = payload["iat"]
    expires_timestamp = payload["exp"]
    if (
        not isinstance(session_id, str)
        or not session_id
        or len(session_id) > 256
        or not isinstance(token_id, str)
        or not token_id
        or len(token_id) > 128
        or not isinstance(issued_timestamp, int)
        or isinstance(issued_timestamp, bool)
        or issued_timestamp < 0
        or not isinstance(expires_timestamp, int)
        or isinstance(expires_timestamp, bool)
        or expires_timestamp <= issued_timestamp
    ):
        raise InvalidControlToken("invalid control token payload")

    try:
        issued_at = datetime.fromtimestamp(issued_timestamp, UTC)
        expires_at = datetime.fromtimestamp(expires_timestamp, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidControlToken("invalid control token payload") from exc
    return session_id, issued_at, expires_at, token_id
