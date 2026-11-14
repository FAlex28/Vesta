from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import pytest

from app.voice import (
    VoiceConnectionStatus,
    VoiceConversationAlreadyActive,
    VoiceConversationError,
    VoiceConversationRegistry,
    VoiceRetryLater,
)

T = TypeVar("T")
NOW = datetime(2026, 11, 14, 16, 30, tzinfo=UTC)


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def test_reserve_is_exclusive_for_connecting_connected_and_reconnecting() -> None:
    async def scenario() -> None:
        registry = VoiceConversationRegistry()
        first = await registry.reserve("session-1", now=NOW)
        assert first.status is VoiceConnectionStatus.CONNECTING
        assert first.reservation_expires_at == NOW + timedelta(seconds=30)
        assert first.version == 1

        with pytest.raises(VoiceConversationAlreadyActive):
            await registry.reserve("session-1", now=NOW + timedelta(seconds=1))

        await registry.token_issued("session-1", "conv-1", now=NOW + timedelta(seconds=2))
        connected = await registry.update(
            "session-1",
            VoiceConnectionStatus.CONNECTED,
            now=NOW + timedelta(seconds=3),
            conversation_id="conv-1",
        )
        assert connected.status is VoiceConnectionStatus.CONNECTED
        with pytest.raises(VoiceConversationAlreadyActive):
            await registry.reserve("session-1", now=NOW + timedelta(seconds=4))

        reconnecting = await registry.update(
            "session-1",
            VoiceConnectionStatus.RECONNECTING,
            now=NOW + timedelta(seconds=5),
            conversation_id="conv-1",
        )
        assert reconnecting.status is VoiceConnectionStatus.RECONNECTING
        with pytest.raises(VoiceConversationAlreadyActive):
            await registry.reserve("session-1", now=NOW + timedelta(seconds=6))

    run(scenario())


def test_expired_connecting_reservation_can_be_replaced() -> None:
    async def scenario() -> None:
        registry = VoiceConversationRegistry(reservation_ttl=timedelta(seconds=5))
        await registry.reserve("session-1", now=NOW)

        replacement = await registry.reserve("session-1", now=NOW + timedelta(seconds=5))

        assert replacement.status is VoiceConnectionStatus.CONNECTING
        assert replacement.conversation_id is None
        assert replacement.version == 2
        assert replacement.reservation_expires_at == NOW + timedelta(seconds=10)

    run(scenario())


def test_token_requires_reservation_and_preserves_conversation_id() -> None:
    async def scenario() -> None:
        registry = VoiceConversationRegistry()
        with pytest.raises(VoiceConversationError, match="not reserved"):
            await registry.token_issued("missing", "conv-1", now=NOW)

        await registry.reserve("session-1", now=NOW)
        issued = await registry.token_issued(
            "session-1", "conv-1", now=NOW + timedelta(milliseconds=10)
        )

        assert issued.status is VoiceConnectionStatus.CONNECTING
        assert issued.conversation_id == "conv-1"
        assert issued.version == 2

    run(scenario())


def test_disconnected_clears_backoff_and_allows_new_reservation() -> None:
    async def scenario() -> None:
        registry = VoiceConversationRegistry()
        await registry.reserve("session-1", now=NOW)
        await registry.token_issued("session-1", "conv-1", now=NOW)
        await registry.update(
            "session-1",
            VoiceConnectionStatus.CONNECTED,
            now=NOW,
            conversation_id="conv-1",
        )
        disconnected = await registry.update(
            "session-1",
            VoiceConnectionStatus.DISCONNECTED,
            now=NOW + timedelta(seconds=1),
            conversation_id="conv-1",
        )

        assert disconnected.consecutive_failures == 0
        assert disconnected.retry_after is None
        next_access = await registry.reserve("session-1", now=NOW + timedelta(seconds=1))
        assert next_access.status is VoiceConnectionStatus.CONNECTING
        assert next_access.conversation_id is None

    run(scenario())


def test_mismatched_conversation_id_is_rejected() -> None:
    async def scenario() -> None:
        registry = VoiceConversationRegistry()
        await registry.reserve("session-1", now=NOW)
        await registry.token_issued("session-1", "conv-1", now=NOW)

        with pytest.raises(VoiceConversationError, match="does not match"):
            await registry.update(
                "session-1",
                VoiceConnectionStatus.CONNECTED,
                now=NOW + timedelta(seconds=1),
                conversation_id="conv-other",
            )

        assert (await registry.get("session-1")).conversation_id == "conv-1"

    run(scenario())


def test_retry_backoff_is_progressive_and_capped() -> None:
    async def scenario() -> None:
        registry = VoiceConversationRegistry(
            retry_base=timedelta(seconds=1),
            retry_cap=timedelta(seconds=4),
        )
        current = NOW
        expected_delays = (1, 2, 4, 4)
        for failure_number, delay_seconds in enumerate(expected_delays, start=1):
            await registry.reserve("session-1", now=current)
            failed = await registry.provider_failed("session-1", now=current, retryable=True)
            expected_retry = current + timedelta(seconds=delay_seconds)
            assert failed.status is VoiceConnectionStatus.SERVICE_UNAVAILABLE
            assert failed.consecutive_failures == failure_number
            assert failed.retry_after == expected_retry

            with pytest.raises(VoiceRetryLater) as retry_error:
                await registry.reserve("session-1", now=expected_retry - timedelta(microseconds=1))
            assert retry_error.value.retry_after == expected_retry
            current = expected_retry

    run(scenario())


def test_non_retryable_provider_failure_uses_api_error_and_backoff() -> None:
    async def scenario() -> None:
        registry = VoiceConversationRegistry()
        await registry.reserve("session-1", now=NOW)

        failed = await registry.provider_failed("session-1", now=NOW, retryable=False)

        assert failed.status is VoiceConnectionStatus.API_ERROR
        assert failed.consecutive_failures == 1
        assert failed.retry_after == NOW + timedelta(milliseconds=250)

    run(scenario())


def test_returned_records_are_defensive_copies() -> None:
    async def scenario() -> None:
        registry = VoiceConversationRegistry()
        reserved = await registry.reserve("session-1", now=NOW)
        reserved.status = VoiceConnectionStatus.CONNECTED
        reserved.conversation_id = "tampered"

        stored = await registry.get("session-1")
        assert stored.status is VoiceConnectionStatus.CONNECTING
        assert stored.conversation_id is None

        missing = await registry.get("missing")
        missing.status = VoiceConnectionStatus.CONNECTED
        assert (await registry.get("missing")).status is VoiceConnectionStatus.DISCONNECTED

    run(scenario())


@pytest.mark.parametrize(
    "arguments",
    [
        {"reservation_ttl": timedelta(0)},
        {"retry_base": timedelta(0)},
        {
            "retry_base": timedelta(seconds=2),
            "retry_cap": timedelta(seconds=1),
        },
    ],
)
def test_registry_rejects_invalid_timing_configuration(
    arguments: dict[str, timedelta],
) -> None:
    with pytest.raises(ValueError):
        VoiceConversationRegistry(**arguments)
