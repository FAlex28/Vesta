from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

import httpx

from app.errors import AppError

LOGGER = logging.getLogger("vesta.assistant")


class AssistantAction(StrEnum):
    NONE = "NONE"  # unclear, or just talking: keep the conversation going
    FALSE_ALARM = "FALSE_ALARM"  # she says the press was a mistake / everything is fine, stop
    ESCALATE = "ESCALATE"  # she asks for help, says it is not a mistake, or is in danger
    CONFIRM_SAFE = "CONFIRM_SAFE"  # she is fine but keeps the session running


class AssistantUnavailable(AppError):
    def __init__(self, message: str = "The voice assistant is not available") -> None:
        super().__init__(code="ASSISTANT_UNAVAILABLE", message=message, status_code=503)


@dataclass(frozen=True, slots=True)
class AssistantDecision:
    transcript: str
    reply: str
    action: AssistantAction


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    id: str
    role: str  # "person" | "assistant"
    text: str
    at: datetime
    action: str | None = None


class Understanding(Protocol):
    async def decide(
        self, *, audio: bytes, mime_type: str, history: list[ConversationTurn], context: str
    ) -> AssistantDecision: ...


class Speech(Protocol):
    async def synthesize(self, text: str) -> tuple[bytes, str]: ...


# ---------------------------------------------------------------------------- scripted lines

LINES: dict[str, str] = {
    "greeting": (
        "Bună, sunt Vesta. Am primit semnalul de la butonul tău. A fost o apăsare din greșeală? "
        "Spune „da, greșeală” sau „nu, am nevoie de ajutor”."
    ),
    "reprompt": (
        "Nu te-am auzit. Ești în siguranță? Dacă nu răspunzi, anunț dispeceratul și persoana ta "
        "de încredere."
    ),
    "checkin": "Sunt tot Vesta. Ești bine? Răspunde-mi, te rog.",
    "alert": (
        "Am anunțat dispeceratul și persoana ta de încredere și le trimit locația ta. Rămân cu "
        "tine. Spune-mi ce se întâmplă, dacă poți."
    ),
    "false_alarm": "Am înțeles, a fost o alarmă falsă. Am oprit Vesta și am șters înregistrarea.",
    "confirm_safe": "Mă bucur că ești bine. Rămân cu tine și te mai întreb din când în când.",
    "unclear": "Nu am înțeles. Poți repeta?",
    "operator": "Te conectez cu un operator de la dispecerat. De acum vorbești direct cu el.",
    "transcription_unavailable": "Nu am putut transcrie ce ai spus. Dacă ai nevoie de ajutor, apasă butonul roșu.",
}

UNTRANSCRIBED_TEXT = "Mesajul nu a putut fi transcris."

# Every ~15 s, while nobody speaks, Vesta keeps her company and gathers what helps the police.
NUDGES: dict[str, tuple[str, ...]] = {
    "ACTIVE": (
        "Sunt aici, cu tine. Ești bine?",
        "Unde ești acum? Spune-mi pe ce stradă sau ce vezi în jur.",
        "Totul în regulă? Spune-mi oricând dacă ai nevoie de ajutor.",
        "Ești singură sau e cineva cu tine?",
    ),
    "ALERT": (
        "Ești bine? Poți vorbi?",
        "Unde te afli? Spune-mi strada, un magazin sau ceva ce vezi în jur.",
        "Ești rănită?",
        "E cineva lângă tine? Cum arată, ce poartă?",
        "Poți ajunge într-un loc sigur, cu oameni în jur?",
        "Dispeceratul te aude. Spune-mi ce se întâmplă, dacă poți.",
    ),
}

SYSTEM_PROMPT = """Ești Vesta, un asistent de siguranță care vorbește în română, prin căștile unei
persoane care a ținut apăsat 5 secunde butonul de urgență. Răspunzi la fiecare frază a ei.

Reguli:
- Răspunsuri foarte scurte (cel mult două propoziții), calme, fără liste și fără emoji.
- Prima ta grijă: află dacă apăsarea a fost din greșeală.
- Nu promite că vine poliția și nu da sfaturi medicale. Spune doar ce face Vesta: anunță
  dispeceratul și persoana de încredere și trimite locația.
- Alege exact o acțiune:
  * FALSE_ALARM doar dacă persoana spune clar că a fost o greșeală, o alarmă falsă sau că vrea
    să oprești Vesta pentru că e bine.
  * ESCALATE dacă cere ajutor, spune că nu e o greșeală, că e în pericol, că îi e frică, îți cere
    să suni la poliție sau la dispecerat, sau dacă ce spune sună a pericol.
  * CONFIRM_SAFE dacă spune că e bine dar nu cere oprirea (sesiunea continuă).
  * NONE dacă nu se înțelege, e doar zgomot sau doar povestește.
- La un pericol, mai bine ESCALATE decât FALSE_ALARM.
- În timpul unei alerte, adună informații utile poliției: unde este (stradă, repere), dacă e
  rănită, cine e lângă ea și cum arată. Pune o singură întrebare scurtă o dată.
- transcript: exact ce a spus persoana, în română ("" dacă nu se aude nimic).
"""

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "transcript": {"type": "STRING"},
        "reply": {"type": "STRING"},
        "action": {"type": "STRING", "enum": [a.value for a in AssistantAction]},
    },
    "required": ["transcript", "reply", "action"],
}


# ---------------------------------------------------------------------------- providers


class GeminiUnderstanding:
    """Gemini via its REST API: audio of one phrase in, a JSON decision out."""

    def __init__(self, api_key: str, model: str, *, timeout_seconds: float = 15.0) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    async def decide(
        self, *, audio: bytes, mime_type: str, history: list[ConversationTurn], context: str
    ) -> AssistantDecision:
        contents: list[dict[str, Any]] = []
        for turn in history[-12:]:
            contents.append(
                {
                    "role": "model" if turn.role == "assistant" else "user",
                    "parts": [{"text": turn.text}],
                }
            )
        contents.append(
            {
                "role": "user",
                "parts": [
                    {"text": f"Context: {context}\nFraza persoanei, ca audio:"},
                    {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(audio).decode()}},
                ],
            }
        )
        body = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": contents,
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _RESPONSE_SCHEMA,
                "temperature": 0.2,
                "maxOutputTokens": 400,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self._model}:generateContent"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=body, headers={"x-goog-api-key": self._api_key})
        except httpx.HTTPError as exc:
            raise AssistantUnavailable(f"Gemini did not answer: {type(exc).__name__}") from None
        if response.status_code != 200:
            LOGGER.warning("Gemini HTTP %s: %s", response.status_code, response.text[:300])
            raise AssistantUnavailable(f"Gemini answered HTTP {response.status_code}")
        try:
            parts = response.json()["candidates"][0]["content"]["parts"]
            data = json.loads("".join(part.get("text", "") for part in parts))
            action = AssistantAction(str(data.get("action", "NONE")).upper())
        except (KeyError, IndexError, ValueError, TypeError):
            LOGGER.warning("Gemini answer could not be parsed: %s", response.text[:300])
            raise AssistantUnavailable("Gemini answer could not be read") from None
        return AssistantDecision(
            transcript=str(data.get("transcript") or "").strip()[:1000],
            reply=str(data.get("reply") or "").strip()[:500] or LINES["unclear"],
            action=action,
        )


class ElevenLabsSpeech:
    """ElevenLabs text-to-speech (mp3). The key stays on the server."""

    def __init__(self, api_key: str, voice_id: str, model: str, *, timeout_seconds: float = 15.0) -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model
        self._timeout = timeout_seconds

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self._voice_id}"
        body = {"text": text, "model_id": self._model, "language_code": "ro"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    url,
                    params={"output_format": "mp3_44100_64"},
                    json=body,
                    headers={"xi-api-key": self._api_key, "accept": "audio/mpeg"},
                )
        except httpx.HTTPError as exc:
            raise AssistantUnavailable(f"ElevenLabs did not answer: {type(exc).__name__}") from None
        if response.status_code != 200:
            LOGGER.warning("ElevenLabs HTTP %s: %s", response.status_code, response.text[:300])
            raise AssistantUnavailable(f"ElevenLabs answered HTTP {response.status_code}")
        return response.content, "audio/mpeg"


# ---------------------------------------------------------------------------- service


@dataclass(slots=True)
class SpokenReply:
    text: str
    audio: bytes | None = None
    mime_type: str | None = None
    turn: ConversationTurn | None = None

    def audio_base64(self) -> str | None:
        return base64.b64encode(self.audio).decode() if self.audio else None


@dataclass(slots=True)
class _Conversation:
    turns: list[ConversationTurn] = field(default_factory=list)


class AssistantService:
    def __init__(
        self,
        broker: Any,
        *,
        understanding: Understanding | None = None,
        speech: Speech | None = None,
        recorder: Any | None = None,
    ) -> None:
        self._broker = broker
        self.understanding = understanding
        self.speech = speech
        self._recorder = recorder
        self._conversations: dict[str, _Conversation] = {}
        self._replies: OrderedDict[str, Any] = OrderedDict()
        self._nudge_count: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @property
    def capabilities(self) -> dict[str, bool]:
        return {"understanding": self.understanding is not None, "voice": self.speech is not None}

    # -- conversation memory ------------------------------------------------------------

    def turns(self, session_id: str) -> list[ConversationTurn]:
        conversation = self._conversations.get(session_id)
        return list(conversation.turns) if conversation else []

    async def forget(self, session_id: str) -> None:
        async with self._lock:
            self._conversations.pop(session_id, None)
            self._nudge_count.pop(session_id, None)

    async def _add_turn(
        self, session_id: str, role: str, text: str, *, action: str | None = None
    ) -> ConversationTurn:
        turn = ConversationTurn(
            id=str(uuid4()), role=role, text=text, at=datetime.now(UTC), action=action
        )
        async with self._lock:
            self._conversations.setdefault(session_id, _Conversation()).turns.append(turn)
        # The event carries no text: dashboard and contact read it only when allowed.
        await self._broker.publish(
            "conversation.updated",
            session_id=session_id,
            data={"turn_id": turn.id, "role": role, "action": action},
        )
        if self._recorder is not None:
            self._recorder(session_id, turn)
        return turn

    def _cached(self, key: str) -> Any | None:
        return self._replies.get(key)

    def _cache(self, key: str, value: Any) -> None:
        self._replies[key] = value
        while len(self._replies) > 200:
            self._replies.popitem(last=False)

    async def _voice(self, text: str) -> tuple[bytes | None, str | None]:
        if self.speech is None:
            return None, None
        try:
            return await self.speech.synthesize(text)
        except AssistantUnavailable:
            # The phone falls back to its own voice; the words are the same.
            return None, None

    # -- what Vesta says ------------------------------------------------------------------

    async def say(
        self, session_id: str, line: str, *, event_id: str, record: bool = True, state: str | None = None
    ) -> SpokenReply:
        cache_key = f"say:{session_id}:{event_id}"
        if (cached := self._cached(cache_key)) is not None:
            return cached
        if line == "nudge":
            options = NUDGES["ALERT" if state == "ALERT" else "ACTIVE"]
            count = self._nudge_count.get(session_id, 0)
            self._nudge_count[session_id] = count + 1
            text = options[count % len(options)]
        else:
            text = LINES[line]
        audio, mime = await self._voice(text)
        turn = await self._add_turn(session_id, "assistant", text) if record else None
        reply = SpokenReply(text=text, audio=audio, mime_type=mime, turn=turn)
        self._cache(cache_key, reply)
        return reply

    # -- what she says --------------------------------------------------------------------

    async def understand(
        self, session_id: str, *, audio: bytes, mime_type: str, context: str, event_id: str
    ) -> tuple[AssistantDecision, SpokenReply]:
        cache_key = f"hear:{session_id}:{event_id}"
        if (cached := self._cached(cache_key)) is not None:
            return cached
        if self.understanding is None:
            return await self._untranscribed(session_id, event_id, cache_key)
        try:
            decision = await self.understanding.decide(
                audio=audio, mime_type=mime_type, history=self.turns(session_id), context=context
            )
        except AssistantUnavailable:
            return await self._untranscribed(session_id, event_id, cache_key)
        if not decision.transcript.strip():
            return await self._untranscribed(session_id, event_id, cache_key)
        reply_text = decision.reply
        if decision.action is AssistantAction.FALSE_ALARM:
            reply_text = LINES["false_alarm"]
        audio_out, mime = await self._voice(reply_text)
        if decision.transcript:
            await self._add_turn(session_id, "person", decision.transcript, action=decision.action.value)
        turn = await self._add_turn(session_id, "assistant", reply_text)
        spoken = SpokenReply(text=reply_text, audio=audio_out, mime_type=mime, turn=turn)
        result = (decision, spoken)
        self._cache(cache_key, result)
        return result

    async def _untranscribed(
        self, session_id: str, event_id: str, cache_key: str
    ) -> tuple[AssistantDecision, SpokenReply]:
        decision = AssistantDecision(
            transcript=UNTRANSCRIBED_TEXT,
            reply=LINES["transcription_unavailable"],
            action=AssistantAction.NONE,
        )
        await self._add_turn(
            session_id, "person", UNTRANSCRIBED_TEXT, action="TRANSCRIPTION_UNAVAILABLE"
        )
        spoken = await self.say(
            session_id,
            "transcription_unavailable",
            event_id=f"{event_id}-transcription-unavailable",
        )
        result = (decision, spoken)
        self._cache(cache_key, result)
        return result
