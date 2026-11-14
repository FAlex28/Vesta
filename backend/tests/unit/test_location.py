from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services.location import (
    AuthorizedLocation,
    InMemoryAuthorizedLocationStore,
    LocationEventReused,
    LocationFromFuture,
)

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)


def _record(
    store: InMemoryAuthorizedLocationStore,
    *,
    event_id: str,
    captured_at: datetime = NOW,
    latitude: float = 44.4268,
    longitude: float = 26.1025,
):
    return store.record(
        event_id=event_id,
        session_id="session-1",
        latitude=latitude,
        longitude=longitude,
        captured_at=captured_at,
        received_at=NOW,
        accuracy_meters=12.5,
        source="android",
    )


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(90.01, 0), (-90.01, 0), (0, 180.01), (0, -180.01)],
)
def test_authorized_location_rejects_invalid_coordinates(
    latitude: float,
    longitude: float,
) -> None:
    with pytest.raises(ValueError):
        AuthorizedLocation(
            session_id="session-1",
            latitude=latitude,
            longitude=longitude,
            captured_at=NOW,
            received_at=NOW,
        )


def test_authorized_location_rejects_naive_time_and_negative_accuracy() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        AuthorizedLocation(
            session_id="session-1",
            latitude=44.4,
            longitude=26.1,
            captured_at=datetime(2026, 11, 14, 10, 0),
            received_at=NOW,
        )
    with pytest.raises(ValueError, match="accuracy"):
        AuthorizedLocation(
            session_id="session-1",
            latitude=44.4,
            longitude=26.1,
            captured_at=NOW,
            received_at=NOW,
            accuracy_meters=-0.1,
        )


def test_location_write_is_idempotent_and_event_reuse_is_rejected() -> None:
    store = InMemoryAuthorizedLocationStore()

    async def scenario() -> None:
        first = await _record(store, event_id="location-1")
        replay = await _record(store, event_id="location-1")
        assert first.duplicate is False
        assert first.accepted_as_latest is True
        assert replay.duplicate is True
        assert replay.location == first.location

        with pytest.raises(LocationEventReused):
            await _record(store, event_id="location-1", latitude=45.0)

    asyncio.run(scenario())


def test_older_location_does_not_replace_latest() -> None:
    store = InMemoryAuthorizedLocationStore()

    async def scenario() -> None:
        latest = await _record(store, event_id="new", captured_at=NOW)
        stale = await _record(
            store,
            event_id="old",
            captured_at=NOW - timedelta(minutes=10),
            latitude=45.0,
        )
        current = await store.get_last_authorized("session-1")
        assert stale.accepted_as_latest is False
        assert stale.location == latest.location
        assert current == latest.location

    asyncio.run(scenario())


def test_location_too_far_in_future_is_rejected() -> None:
    store = InMemoryAuthorizedLocationStore(max_future_skew=timedelta(seconds=30))

    async def scenario() -> None:
        with pytest.raises(LocationFromFuture):
            await _record(
                store,
                event_id="future",
                captured_at=NOW + timedelta(seconds=31),
            )

    asyncio.run(scenario())
