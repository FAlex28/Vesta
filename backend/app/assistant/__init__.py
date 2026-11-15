"""Vesta's voice assistant: talks with the person through her earbuds.

- Gemini listens (the phone sends each spoken phrase as WAV) and returns what she said, what to
  answer and one of four explicit intents.
- ElevenLabs gives the answer a voice; without its key the phone speaks with the browser voice.
- Without a Gemini key nothing is interpreted: the phone shows the questions as text and the
  person answers with the on-screen buttons. The safety timers work the same either way.

The assistant never changes the session itself. The API layer applies an intent only through
the normal safety commands (cancel as FALSE_ALARM, SOS, confirmation of the open check-in).
"""

from .service import (
    AssistantAction,
    AssistantDecision,
    AssistantService,
    AssistantUnavailable,
    ConversationTurn,
    ElevenLabsSpeech,
    GeminiUnderstanding,
)

__all__ = [
    "AssistantAction",
    "AssistantDecision",
    "AssistantService",
    "AssistantUnavailable",
    "ConversationTurn",
    "ElevenLabsSpeech",
    "GeminiUnderstanding",
]
