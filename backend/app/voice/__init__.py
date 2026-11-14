"""Voice-provider boundary for Vesta.

Only short-lived conversation credentials cross this boundary.  The permanent
ElevenLabs API key remains inside :class:`ElevenLabsVoiceProvider`.
"""

from .prompt import (
    CLIENT_TOOL_SPECS,
    ROMANIAN_FIRST_MESSAGE,
    ROMANIAN_SYSTEM_PROMPT,
    ClientToolSpec,
)
from .provider import (
    DemoVoiceProvider,
    ElevenLabsVoiceProvider,
    VoiceAccess,
    VoiceErrorCode,
    VoiceMode,
    VoiceProvider,
    VoiceProviderError,
    VoiceTransport,
)
from .sessions import (
    VoiceConnectionStatus,
    VoiceConversationAlreadyActive,
    VoiceConversationError,
    VoiceConversationRegistry,
    VoiceRetryLater,
    VoiceSessionRecord,
)

__all__ = [
    "CLIENT_TOOL_SPECS",
    "ROMANIAN_FIRST_MESSAGE",
    "ROMANIAN_SYSTEM_PROMPT",
    "ClientToolSpec",
    "DemoVoiceProvider",
    "ElevenLabsVoiceProvider",
    "VoiceAccess",
    "VoiceErrorCode",
    "VoiceMode",
    "VoiceProvider",
    "VoiceProviderError",
    "VoiceTransport",
    "VoiceConnectionStatus",
    "VoiceConversationAlreadyActive",
    "VoiceConversationError",
    "VoiceConversationRegistry",
    "VoiceRetryLater",
    "VoiceSessionRecord",
]
