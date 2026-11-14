"""Device-scoped access for the protected person's phone and her emergency contacts.

* The phone pairs once with the **device pairing code** printed on the Raspberry Pi
  (``VESTA_DEVICE_PAIRING_CODE``, shown as a QR code by the install script). The
  dispatcher's dashboard does not hand out codes.
* The paired phone adds emergency contacts and creates a one-time **invite code** for each;
  the phone itself sends it by SMS. The contact claims it once.
* A claim returns a signed bearer grant (``g1.<payload>.<signature>``) scoped to one device
  and one role. Grants are also listed in :class:`~app.services.directory.DeviceDirectory`,
  so the person can revoke a phone or a contact at any time.

Grants use a key derived from VESTA_CONTROL_SIGNING_SECRET, so they never verify as session
control tokens and they survive a restart when the secret is stable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from app.errors import AppError, AuthenticationError

_TOKEN_VERSION = "g1"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L


class AccessScope(StrEnum):
    COMPANION = "companion"
    CONTACT = "contact"


class TooManyAttemptsError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="TOO_MANY_ATTEMPTS",
            message="Too many invalid codes; wait a minute",
            status_code=429,
        )


@dataclass(frozen=True, slots=True)
class AccessGrant:
    scope: AccessScope
    device_id: str
    expires_at: datetime
    token_id: str


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(UTC)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def normalize_code(raw: str) -> str:
    """Case, spaces and dashes do not matter when a person types a code."""

    return "".join(ch for ch in raw.upper() if ch.isalnum())


def generate_code(length: int) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def codes_match(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(normalize_code(supplied).encode(), normalize_code(expected).encode())


class AttemptLimiter:
    """Global limit on wrong codes per minute (brute-force guard for typed codes)."""

    def __init__(self, *, max_failures_per_minute: int = 10) -> None:
        self._failures: deque[datetime] = deque()
        self._max = max_failures_per_minute

    def check(self, now: datetime | None = None) -> None:
        instant = _utc(now)
        while self._failures and instant - self._failures[0] > timedelta(minutes=1):
            self._failures.popleft()
        if len(self._failures) >= self._max:
            raise TooManyAttemptsError()

    def failed(self, now: datetime | None = None) -> None:
        self._failures.append(_utc(now))


class AccessTokenService:
    __slots__ = ("_key",)

    def __init__(self, secret: str | bytes) -> None:
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(secret_bytes) < 32:
            raise ValueError("access-token signing secret must contain at least 32 bytes")
        self._key = hmac.new(secret_bytes, b"vesta-device-access-v1", hashlib.sha256).digest()

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def issue(
        self,
        scope: AccessScope,
        device_id: str,
        *,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> tuple[str, datetime, str]:
        """Return (token, expires_at, token_id)."""

        issued = _utc(now)
        expires = issued + ttl
        token_id = secrets.token_urlsafe(12)
        payload = {
            "did": device_id,
            "exp": int(expires.timestamp()),
            "iat": int(issued.timestamp()),
            "jti": token_id,
            "scp": AccessScope(scope).value,
        }
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signed = f"{_TOKEN_VERSION}.{body}"
        return (
            f"{signed}.{self._sign(signed)}",
            datetime.fromtimestamp(payload["exp"], UTC),
            token_id,
        )

    def verify(self, token: str | None, scope: AccessScope, *, now: datetime | None = None) -> AccessGrant:
        if not token or len(token) > 2048:
            raise AuthenticationError()
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != _TOKEN_VERSION:
            raise AuthenticationError()
        signed = f"{parts[0]}.{parts[1]}"
        if not hmac.compare_digest(self._sign(signed), parts[2]):
            raise AuthenticationError()
        try:
            payload = json.loads(_unb64(parts[1]))
            granted_scope = AccessScope(payload["scp"])
            device_id = str(payload["did"])
            expires_at = datetime.fromtimestamp(int(payload["exp"]), UTC)
            token_id = str(payload["jti"])
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthenticationError() from exc
        if granted_scope is not scope or not device_id:
            raise AuthenticationError()
        if _utc(now) >= expires_at:
            raise AuthenticationError("Access token has expired")
        return AccessGrant(granted_scope, device_id, expires_at, token_id)

    def _sign(self, signed: str) -> str:
        return _b64(hmac.new(self._key, signed.encode("ascii"), hashlib.sha256).digest())
