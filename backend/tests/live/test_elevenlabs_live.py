from __future__ import annotations

import asyncio
import os
import time

import pytest

from app.voice import ElevenLabsVoiceProvider, VoiceMode, VoiceTransport

pytestmark = pytest.mark.live


def _live_enabled() -> bool:
    return bool(
        os.getenv("RUN_ELEVENLABS_LIVE") == "1"
        and os.getenv("ELEVENLABS_API_KEY")
        and os.getenv("ELEVENLABS_AGENT_ID")
    )


@pytest.mark.skipif(
    not _live_enabled(),
    reason="set RUN_ELEVENLABS_LIVE=1 and ElevenLabs credentials",
)
def test_live_webrtc_token_can_be_minted_within_latency_budget() -> None:
    async def scenario() -> None:
        provider = ElevenLabsVoiceProvider(
            api_key=os.environ["ELEVENLABS_API_KEY"],
            agent_id=os.environ["ELEVENLABS_AGENT_ID"],
            timeout_seconds=10.0,
            max_retries=1,
        )
        try:
            started = time.perf_counter()
            access = await provider.create_access(
                session_id="authorized-live-smoke-test",
                participant_name="vesta-backend-smoke-test",
                environment=os.getenv("ELEVENLABS_ENVIRONMENT"),
            )
            elapsed = time.perf_counter() - started
        finally:
            await provider.aclose()

        latency_budget = float(os.getenv("ELEVENLABS_LIVE_MAX_TOKEN_LATENCY_SECONDS", "10"))
        assert access.mode is VoiceMode.LIVE
        assert access.transport is VoiceTransport.WEBRTC
        assert access.conversation_token
        assert access.conversation_id
        assert elapsed <= latency_budget, (
            f"WebRTC token latency {elapsed:.3f}s exceeded {latency_budget:.3f}s"
        )

    asyncio.run(scenario())
