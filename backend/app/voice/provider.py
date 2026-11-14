"""ElevenLabs credential minting and a deterministic demo provider."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import httpx

from .prompt import (
    CLIENT_TOOL_SPECS,
    ROMANIAN_FIRST_MESSAGE,
    ROMANIAN_SYSTEM_PROMPT,
    ClientToolSpec,
)


class VoiceMode(StrEnum):
    LIVE = "live"
    DEMO = "demo"


class VoiceTransport(StrEnum):
    WEBRTC = "webrtc"
    SCRIPTED = "scripted"


class VoiceErrorCode(StrEnum):
    NOT_CONFIGURED = "not_configured"
    AUTHENTICATION_FAILED = "authentication_failed"
    QUOTA_UNAVAILABLE = "quota_unavailable"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


_PUBLIC_ERROR_MESSAGES = {
    VoiceErrorCode.NOT_CONFIGURED: "Serviciul vocal nu este configurat.",
    VoiceErrorCode.AUTHENTICATION_FAILED: "Autentificarea serviciului vocal a eșuat.",
    VoiceErrorCode.QUOTA_UNAVAILABLE: "Serviciul vocal nu are cotă disponibilă.",
    VoiceErrorCode.RATE_LIMITED: "Serviciul vocal este momentan ocupat.",
    VoiceErrorCode.PROVIDER_UNAVAILABLE: "Serviciul vocal este indisponibil momentan.",
}


class VoiceProviderError(RuntimeError):
    """Sanitized provider failure safe to map to an API response.

    Provider response bodies and request headers are intentionally not retained,
    preventing the permanent API key from leaking through logs or exceptions.
    """

    def __init__(
        self,
        code: VoiceErrorCode,
        *,
        retryable: bool,
        provider_status: int | None = None,
    ) -> None:
        super().__init__(_PUBLIC_ERROR_MESSAGES[code])
        self.code = code
        self.retryable = retryable
        self.provider_status = provider_status


@dataclass(frozen=True, slots=True)
class VoiceAccess:
    """Short-lived client access plus non-secret conversation metadata."""

    provider: str
    mode: VoiceMode
    transport: VoiceTransport
    conversation_token: str | None = field(default=None, repr=False)
    conversation_id: str | None = None
    language: str = "ro"
    first_message: str = ROMANIAN_FIRST_MESSAGE
    system_prompt: str = ROMANIAN_SYSTEM_PROMPT
    client_tools: tuple[ClientToolSpec, ...] = CLIENT_TOOL_SPECS
    demo_messages: tuple[str, ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        """Return an API-ready object; permanent credentials are never present."""

        payload: dict[str, Any] = {
            "provider": self.provider,
            "mode": self.mode.value,
            "transport": self.transport.value,
            "language": self.language,
            "first_message": self.first_message,
            "system_prompt": self.system_prompt,
            "client_tools": [tool.to_public_dict() for tool in self.client_tools],
        }
        if self.conversation_token is not None:
            payload["conversation_token"] = self.conversation_token
        if self.conversation_id is not None:
            payload["conversation_id"] = self.conversation_id
        if self.demo_messages:
            payload["demo_messages"] = list(self.demo_messages)
        return payload


@runtime_checkable
class VoiceProvider(Protocol):
    async def create_access(
        self,
        *,
        session_id: str,
        participant_name: str | None = None,
        environment: str | None = None,
    ) -> VoiceAccess:
        """Mint one short-lived client credential for a Vesta session."""

    async def aclose(self) -> None:
        """Release provider-owned network resources."""


class DemoVoiceProvider:
    """Offline scripted voice mode, explicitly marked as a simulation."""

    _MESSAGES = (
        ROMANIAN_FIRST_MESSAGE,
        "Ești în regulă? Spune-mi clar dacă ești bine sau dacă ai nevoie de ajutor.",
        "Am înțeles. Rămân aici cu tine.",
    )

    async def create_access(
        self,
        *,
        session_id: str,
        participant_name: str | None = None,
        environment: str | None = None,
    ) -> VoiceAccess:
        del participant_name, environment
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        return VoiceAccess(
            provider="demo",
            mode=VoiceMode.DEMO,
            transport=VoiceTransport.SCRIPTED,
            conversation_id=f"demo-{session_id}",
            demo_messages=self._MESSAGES,
        )

    async def aclose(self) -> None:
        return None


Sleep = Callable[[float], Awaitable[None]]


class ElevenLabsVoiceProvider:
    """Mint WebRTC tokens without proxying audio through the Vesta backend."""

    _TOKEN_PATH = "/v1/convai/conversation/token"
    _RETRYABLE_STATUSES = frozenset({429, 503})

    def __init__(
        self,
        *,
        api_key: str | None,
        agent_id: str | None,
        client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.elevenlabs.io",
        timeout_seconds: float = 5.0,
        max_retries: int = 1,
        retry_delay_seconds: float = 0.15,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries not in (0, 1):
            raise ValueError("max_retries must be 0 or 1")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must not be negative")

        self._api_key = (api_key or "").strip()
        self._agent_id = (agent_id or "").strip()
        self._sleep = sleep
        self._max_retries = max_retries
        self._retry_delay_seconds = retry_delay_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(configured={bool(self._api_key and self._agent_id)}, "
            f"max_retries={self._max_retries})"
        )

    async def create_access(
        self,
        *,
        session_id: str,
        participant_name: str | None = None,
        environment: str | None = None,
    ) -> VoiceAccess:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        if not self._api_key or not self._agent_id:
            raise VoiceProviderError(
                VoiceErrorCode.NOT_CONFIGURED,
                retryable=False,
            )

        params = {"agent_id": self._agent_id}
        if participant_name is not None:
            participant_name = participant_name.strip()
            if participant_name:
                params["participant_name"] = participant_name
        if environment is not None:
            environment = environment.strip()
            if environment:
                params["environment"] = environment

        response = await self._request_token(params)
        try:
            data = response.json()
        except ValueError:
            raise VoiceProviderError(
                VoiceErrorCode.PROVIDER_UNAVAILABLE,
                retryable=True,
                provider_status=response.status_code,
            ) from None

        token = data.get("token") if isinstance(data, dict) else None
        conversation_id = data.get("conversation_id") if isinstance(data, dict) else None
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(conversation_id, str)
            or not conversation_id
        ):
            raise VoiceProviderError(
                VoiceErrorCode.PROVIDER_UNAVAILABLE,
                retryable=True,
                provider_status=response.status_code,
            )

        return VoiceAccess(
            provider="elevenlabs",
            mode=VoiceMode.LIVE,
            transport=VoiceTransport.WEBRTC,
            conversation_token=token,
            conversation_id=conversation_id,
        )

    async def _request_token(self, params: dict[str, str]) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(
                    self._TOKEN_PATH,
                    params=params,
                    headers={"xi-api-key": self._api_key},
                )
            except httpx.RequestError:
                raise VoiceProviderError(
                    VoiceErrorCode.PROVIDER_UNAVAILABLE,
                    retryable=True,
                ) from None

            if response.is_success:
                return response

            if response.status_code in self._RETRYABLE_STATUSES and attempt < self._max_retries:
                await self._sleep(self._retry_delay_seconds * (attempt + 1))
                continue

            raise self._error_for_status(response.status_code)

        raise AssertionError("unreachable")

    @staticmethod
    def _error_for_status(status: int) -> VoiceProviderError:
        if status in (401, 403):
            return VoiceProviderError(
                VoiceErrorCode.AUTHENTICATION_FAILED,
                retryable=False,
                provider_status=status,
            )
        if status == 402:
            return VoiceProviderError(
                VoiceErrorCode.QUOTA_UNAVAILABLE,
                retryable=False,
                provider_status=status,
            )
        if status == 404:
            return VoiceProviderError(
                VoiceErrorCode.NOT_CONFIGURED,
                retryable=False,
                provider_status=status,
            )
        if status == 429:
            return VoiceProviderError(
                VoiceErrorCode.RATE_LIMITED,
                retryable=True,
                provider_status=status,
            )
        return VoiceProviderError(
            VoiceErrorCode.PROVIDER_UNAVAILABLE,
            retryable=status >= 500,
            provider_status=status,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
