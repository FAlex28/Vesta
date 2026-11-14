"""Device-scoped access for the paired phone and the trusted contact.

Two pieces, both independent of Tiger Data so they work in memory mode too:

* ``PairingCodeRegistry`` issues short, one-time codes. The operator creates one
  from the authenticated dashboard; the phone (or the contact) types it once.
  Codes live only in memory, expire after a few minutes and are rate-limited.
* ``AccessTokenService`` turns a claimed code into a signed bearer token scoped
  to one device and one role ("companion" or "contact"). A companion token can
  fetch the control token of that device's current session; a contact token
  can only read that device's alerts. Neither works for another device, and a
  session_id on its own still authorizes nothing.

Tokens use the same HMAC secret as control tokens (VESTA_CONTROL_SIGNING_SECRET)
through a separate derived key, so they survive a restart when the secret is set.
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
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
_CODE_LENGTH = 8


class AccessScope(StrEnum):
    COMPANION = "companion"
    CONTACT = "contact"


class TooManyAttemptsError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="TOO_MANY_ATTEMPTS",
            message="Too many invalid pairing codes; wait a minute",
            status_code=429,
        )


@dataclass(frozen=True, slots=True)
class AccessGrant:
    scope: AccessScope
    device_id: str
    expires_at: datetime
    token_id: str


@dataclass(slots=True)
class _PendingCode:
    scope: AccessScope
    device_id: str
    expires_at: datetime


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
    return "".join(ch for ch in raw.upper() if ch.isalnum())


class AccessTokenService:
    __slots__ = ("_key",)

    def __init__(self, secret: str | bytes) -> None:
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(secret_bytes) < 32:
            raise ValueError("access-token signing secret must contain at least 32 bytes")
        # Derived key: an access token can never verify as a control token or vice versa.
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
    ) -> tuple[str, datetime]:
        issued = _utc(now)
        expires = issued + ttl
        payload = {
            "did": device_id,
            "exp": int(expires.timestamp()),
            "iat": int(issued.timestamp()),
            "jti": secrets.token_urlsafe(12),
            "scp": AccessScope(scope).value,
        }
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signed = f"{_TOKEN_VERSION}.{body}"
        return f"{signed}.{self._sign(signed)}", datetime.fromtimestamp(payload["exp"], UTC)

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


class PairingCodeRegistry:
    """One-time pairing codes kept in memory (a restart simply invalidates them)."""

    def __init__(
        self,
        *,
        max_failures_per_minute: int = 10,
        max_pending: int = 50,
    ) -> None:
        self._codes: dict[str, _PendingCode] = {}
        self._failures: deque[datetime] = deque()
        self._max_failures = max_failures_per_minute
        self._max_pending = max_pending

    def issue(
        self,
        scope: AccessScope,
        device_id: str,
        *,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        instant = _utc(now)
        self._purge(instant)
        if len(self._codes) >= self._max_pending:
            oldest = min(self._codes, key=lambda key: self._codes[key].expires_at)
            del self._codes[oldest]
        while True:
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
            if code not in self._codes:
                break
        expires = instant + ttl
        self._codes[code] = _PendingCode(AccessScope(scope), device_id, expires)
        return code, expires

    def claim(self, raw_code: str, *, now: datetime | None = None) -> tuple[AccessScope, str]:
        instant = _utc(now)
        self._purge(instant)
        while self._failures and instant - self._failures[0] > timedelta(minutes=1):
            self._failures.popleft()
        if len(self._failures) >= self._max_failures:
            raise TooManyAttemptsError()
        pending = self._codes.pop(normalize_code(raw_code), None)
        if pending is None:
            self._failures.append(instant)
            raise AuthenticationError("Pairing code is invalid or expired")
        return pending.scope, pending.device_id

    def _purge(self, now: datetime) -> None:
        for code in [code for code, item in self._codes.items() if item.expires_at <= now]:
            del self._codes[code]
