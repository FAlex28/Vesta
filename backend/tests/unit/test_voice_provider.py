from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import httpx
import pytest

from app.voice import (
    CLIENT_TOOL_SPECS,
    DemoVoiceProvider,
    ElevenLabsVoiceProvider,
    VoiceErrorCode,
    VoiceMode,
    VoiceProviderError,
    VoiceTransport,
)


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)


def test_elevenlabs_mints_webrtc_token_with_server_side_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"token": "temporary-token", "conversation_id": "conv-123"},
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.elevenlabs.io",
        ) as client:
            provider = ElevenLabsVoiceProvider(
                api_key="permanent-secret",
                agent_id="agent-1",
                client=client,
                max_retries=0,
            )
            access = await provider.create_access(
                session_id="session-1",
                participant_name="Utilizatoare demo",
                environment="android",
            )

            assert access.mode is VoiceMode.LIVE
            assert access.transport is VoiceTransport.WEBRTC
            assert access.conversation_token == "temporary-token"
            assert access.conversation_id == "conv-123"
            public = access.to_public_dict()
            assert public["language"] == "ro"
            assert public["conversation_token"] == "temporary-token"
            assert "permanent-secret" not in repr(access)
            assert "permanent-secret" not in str(public)

    run(scenario())

    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/v1/convai/conversation/token"
    assert request.url.params["agent_id"] == "agent-1"
    assert request.url.params["participant_name"] == "Utilizatoare demo"
    assert request.url.params["environment"] == "android"
    assert request.headers["xi-api-key"] == "permanent-secret"


@pytest.mark.parametrize(
    ("status", "expected_code", "retryable"),
    [
        (401, VoiceErrorCode.AUTHENTICATION_FAILED, False),
        (403, VoiceErrorCode.AUTHENTICATION_FAILED, False),
        (402, VoiceErrorCode.QUOTA_UNAVAILABLE, False),
        (404, VoiceErrorCode.NOT_CONFIGURED, False),
        (429, VoiceErrorCode.RATE_LIMITED, True),
        (500, VoiceErrorCode.PROVIDER_UNAVAILABLE, True),
        (503, VoiceErrorCode.PROVIDER_UNAVAILABLE, True),
        (400, VoiceErrorCode.PROVIDER_UNAVAILABLE, False),
    ],
)
def test_elevenlabs_maps_provider_errors_without_leaking_response(
    status: int,
    expected_code: VoiceErrorCode,
    retryable: bool,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="provider diagnostic permanent-secret")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.elevenlabs.io",
        ) as client:
            provider = ElevenLabsVoiceProvider(
                api_key="permanent-secret",
                agent_id="agent-1",
                client=client,
                max_retries=0,
            )
            with pytest.raises(VoiceProviderError) as captured:
                await provider.create_access(session_id="session-1")

            error = captured.value
            assert error.code is expected_code
            assert error.retryable is retryable
            assert error.provider_status == status
            assert "permanent-secret" not in str(error)
            assert "provider diagnostic" not in str(error)

    run(scenario())


def test_elevenlabs_retries_429_once_then_succeeds() -> None:
    calls = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"token": "temp", "conversation_id": "conv"})

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.elevenlabs.io",
        ) as client:
            provider = ElevenLabsVoiceProvider(
                api_key="secret",
                agent_id="agent",
                client=client,
                max_retries=1,
                retry_delay_seconds=0.25,
                sleep=fake_sleep,
            )
            access = await provider.create_access(session_id="session")
            assert access.conversation_id == "conv"

    run(scenario())
    assert calls == 2
    assert delays == [0.25]


def test_elevenlabs_timeout_is_sanitized_unavailable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret transport details", request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.elevenlabs.io",
        ) as client:
            provider = ElevenLabsVoiceProvider(
                api_key="secret",
                agent_id="agent",
                client=client,
            )
            with pytest.raises(VoiceProviderError) as captured:
                await provider.create_access(session_id="session")
            assert captured.value.code is VoiceErrorCode.PROVIDER_UNAVAILABLE
            assert captured.value.retryable is True
            assert "transport details" not in str(captured.value)

    run(scenario())


def test_elevenlabs_rejects_missing_configuration_without_request() -> None:
    provider = ElevenLabsVoiceProvider(api_key=None, agent_id=None)

    async def scenario() -> None:
        try:
            with pytest.raises(VoiceProviderError) as captured:
                await provider.create_access(session_id="session")
            assert captured.value.code is VoiceErrorCode.NOT_CONFIGURED
            assert captured.value.retryable is False
            assert "configured=False" in repr(provider)
        finally:
            await provider.aclose()

    run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"token": "only-token"},
        {"conversation_id": "only-id"},
        {"token": "", "conversation_id": "conv"},
    ],
)
def test_elevenlabs_rejects_incomplete_success_payload(payload: dict[str, str]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.elevenlabs.io",
        ) as client:
            provider = ElevenLabsVoiceProvider(
                api_key="secret",
                agent_id="agent",
                client=client,
            )
            with pytest.raises(VoiceProviderError) as captured:
                await provider.create_access(session_id="session")
            assert captured.value.code is VoiceErrorCode.PROVIDER_UNAVAILABLE

    run(scenario())


def test_demo_provider_is_explicitly_scripted_and_safe() -> None:
    access = run(DemoVoiceProvider().create_access(session_id="session-1"))

    assert access.mode is VoiceMode.DEMO
    assert access.transport is VoiceTransport.SCRIPTED
    assert access.conversation_token is None
    assert access.conversation_id == "demo-session-1"
    assert access.demo_messages
    assert "motorul determinist Python" in access.system_prompt
    assert {tool.name for tool in CLIENT_TOOL_SPECS} == {
        "vesta_confirm_safe",
        "vesta_request_help",
        "vesta_report_technical_issue",
    }
