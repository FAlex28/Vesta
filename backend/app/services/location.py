"""Authorized last-known-location boundary used by Agent 2 alert requests."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


class LocationRegistryError(ValueError):
    pass


class LocationEventReused(LocationRegistryError):
    pass


class LocationFromFuture(LocationRegistryError):
    pass


@dataclass(frozen=True, slots=True)
class AuthorizedLocation:
    session_id: str
    latitude: float
    longitude: float
    captured_at: datetime
    received_at: datetime
    accuracy_meters: float | None = None
    source: str = "android"

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if not -90 <= self.latitude <= 90:
            raise ValueError("latitude must be between -90 and 90")
        if not -180 <= self.longitude <= 180:
            raise ValueError("longitude must be between -180 and 180")
        if self.accuracy_meters is not None and self.accuracy_meters < 0:
            raise ValueError("accuracy_meters must not be negative")
        _require_aware(self.captured_at)
        _require_aware(self.received_at)
        if not self.source.strip():
            raise ValueError("source must not be empty")


@dataclass(frozen=True, slots=True)
class LocationUpdateResult:
    location: AuthorizedLocation
    duplicate: bool
    accepted_as_latest: bool


@runtime_checkable
class AuthorizedLocationProvider(Protocol):
    async def get_last_authorized(self, session_id: str) -> AuthorizedLocation | None:
        """Return only a location previously accepted for this session."""


@runtime_checkable
class AuthorizedLocationStore(AuthorizedLocationProvider, Protocol):
    async def record(
        self,
        *,
        event_id: str,
        session_id: str,
        latitude: float,
        longitude: float,
        captured_at: datetime,
        received_at: datetime,
        accuracy_meters: float | None,
        source: str,
    ) -> LocationUpdateResult: ...


class NoopAuthorizedLocationProvider:
    async def get_last_authorized(self, session_id: str) -> AuthorizedLocation | None:
        del session_id
        return None


class InMemoryAuthorizedLocationStore:
    """Integration adapter; Tiger persistence remains another role's boundary."""

    def __init__(self, *, max_future_skew: timedelta = timedelta(minutes=5)) -> None:
        if max_future_skew < timedelta(0):
            raise ValueError("max_future_skew must not be negative")
        self._max_future_skew = max_future_skew
        self._latest: dict[str, AuthorizedLocation] = {}
        self._processed: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def record(
        self,
        *,
        event_id: str,
        session_id: str,
        latitude: float,
        longitude: float,
        captured_at: datetime,
        received_at: datetime,
        accuracy_meters: float | None,
        source: str,
    ) -> LocationUpdateResult:
        instant = _as_utc(received_at)
        captured = _as_utc(captured_at)
        if captured > instant + self._max_future_skew:
            raise LocationFromFuture("location timestamp is too far in the future")
        fingerprint = _fingerprint(
            session_id,
            repr(latitude),
            repr(longitude),
            captured.isoformat(),
            repr(accuracy_meters),
            source,
        )
        async with self._lock:
            prior = self._processed.get(event_id)
            if prior is not None:
                prior_fingerprint, prior_session_id = prior
                if not hmac.compare_digest(prior_fingerprint, fingerprint):
                    raise LocationEventReused("event_id was already used for a different location")
                existing = self._latest.get(prior_session_id)
                if existing is None:
                    raise RuntimeError("processed location is missing its session record")
                return LocationUpdateResult(
                    existing,
                    duplicate=True,
                    accepted_as_latest=False,
                )

            location = AuthorizedLocation(
                session_id=session_id,
                latitude=latitude,
                longitude=longitude,
                captured_at=captured,
                received_at=instant,
                accuracy_meters=accuracy_meters,
                source=source,
            )
            current = self._latest.get(session_id)
            accepted = current is None or captured >= current.captured_at
            if accepted:
                self._latest[session_id] = location
            self._processed[event_id] = (fingerprint, session_id)
            return LocationUpdateResult(
                copy.deepcopy(self._latest[session_id]),
                duplicate=False,
                accepted_as_latest=accepted,
            )

    async def get_last_authorized(self, session_id: str) -> AuthorizedLocation | None:
        async with self._lock:
            return copy.deepcopy(self._latest.get(session_id))

    async def forget(self, session_id: str) -> None:
        """False alarm: the session's positions are deleted, not just hidden."""

        async with self._lock:
            self._latest.pop(session_id, None)
            for event_id in [e for e, (_, sid) in self._processed.items() if sid == session_id]:
                del self._processed[event_id]


def _fingerprint(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _as_utc(value: datetime) -> datetime:
    _require_aware(value)
    return value.astimezone(UTC)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("location timestamps must be timezone-aware")
