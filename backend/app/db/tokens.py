"""Tokenuri de acces cu durată limitată. În DB stă doar SHA-256(token).
Tokenul se trimite în header (Authorization: Bearer ...), niciodată în URL sau în loguri."""

from __future__ import annotations

import hashlib
import secrets
from typing import Literal

from .errors import Forbidden, Unauthorized
from .events import as_uuid

Role = Literal["USER", "CONTACT", "OPERATOR", "PUBLIC_DEMO"]
Action = Literal["control", "read_location", "read_history", "ack_notification"]

# Ce poate face fiecare rol (în plus, verificarea de domeniu de mai jos).
PERMISSIONS: dict[str, set[str]] = {
    "USER": {"control", "read_location", "read_history"},
    "CONTACT": {"read_location", "read_history", "ack_notification"},
    "OPERATOR": {"read_location", "read_history"},
    "PUBLIC_DEMO": {"read_location", "read_history"},
}


def _hash(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("utf-8")).digest()


class TokensRepo:
    def __init__(self, pool, settings):
        self._pool = pool
        self._s = settings

    def issue(
        self,
        role: Role,
        owner_id: str,
        *,
        session_id=None,
        ttl_minutes: float | None = None,
        label: str | None = None,
    ) -> str:
        """Întoarce tokenul în clar O SINGURĂ DATĂ; nu se mai poate recupera din DB."""
        raw = secrets.token_urlsafe(32)
        ttl = self._s.token_ttl_minutes if ttl_minutes is None else ttl_minutes
        with self._pool.autocommit() as tx:
            tx.exec(
                "INSERT INTO access_tokens (token_hash, role, owner_id, session_id, label, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s::double precision))",
                (_hash(raw), role, owner_id, as_uuid(session_id), label, ttl * 60),
            )
        return raw

    def verify(self, raw: str | None) -> dict:
        if not raw:
            raise Unauthorized("token lipsă")
        with self._pool.autocommit() as tx:
            row = tx.one(
                "UPDATE access_tokens SET last_used_at = now() WHERE token_hash = %s "
                "AND revoked_at IS NULL AND expires_at > now() "
                "RETURNING role, owner_id, session_id, expires_at, label",
                (_hash(raw),),
            )
        if row is None:
            raise Unauthorized("token invalid, expirat sau revocat")
        return row

    def revoke(self, raw: str) -> bool:
        with self._pool.autocommit() as tx:
            return (
                tx.exec(
                    "UPDATE access_tokens SET revoked_at = now() WHERE token_hash = %s "
                    "AND revoked_at IS NULL",
                    (_hash(raw),),
                )
                > 0
            )

    def authorize(self, raw: str | None, session_id, action: Action) -> dict:
        """Verifică tokenul ȘI dreptul pe sesiunea cerută. session_id singur nu dă acces la nimic."""
        tok = self.verify(raw)
        if action not in PERMISSIONS[tok["role"]]:
            raise Forbidden(f"rolul {tok['role']} nu poate face {action}")
        with self._pool.autocommit() as tx:
            s = tx.one(
                "SELECT owner_id, simulated FROM sessions WHERE session_id = %s",
                (as_uuid(session_id),),
            )
        if s is None:
            # același răspuns ca la lipsa dreptului: nu confirmăm existența sesiunilor altora
            raise Forbidden("acces refuzat")
        role = tok["role"]
        ok = (
            (role in ("USER", "OPERATOR") and s["owner_id"] == tok["owner_id"])
            or (role == "CONTACT" and tok["session_id"] == as_uuid(session_id))
            or (role == "PUBLIC_DEMO" and s["simulated"])
        )
        if not ok:
            raise Forbidden("acces refuzat")
        return tok
