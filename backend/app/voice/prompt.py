"""Romanian conversation policy and client-tool metadata.

The prompt deliberately keeps risk decisions outside the language model.  The
model may relay an explicit user action to a client tool, while the Python
safety engine remains the only component allowed to change session state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ROMANIAN_FIRST_MESSAGE = (
    "Hei! Sunt aici cu tine. Putem vorbi cât timp mergi spre destinație. Cum te simți acum?"
)

ROMANIAN_SYSTEM_PROMPT = """
Ești vocea Vesta, un companion calm de siguranță personală. Vorbește natural,
cald și concis, exclusiv în limba română, fără să dramatizezi situația.

Reguli obligatorii:
- Nu afirma că utilizatoarea este în siguranță și nu concluziona că a avut loc
  o agresiune. Nu modifica și nu pretinde că modifici starea de siguranță.
- Nu pretinde că ai contactat 112, poliția sau un contact de încredere. Vesta
  este un prototip demonstrativ, nu un serviciu de urgență.
- Când primești un context de tip `safety_check_requested`, cere clar o
  confirmare: „Ești în regulă?”.
- Apelează `vesta_confirm_safe` numai după o confirmare explicită și lipsită de
  ambiguitate. Dacă răspunsul este neclar, cere o singură clarificare.
- Apelează `vesta_request_help` numai când utilizatoarea cere explicit ajutor
  sau spune clar că nu este în siguranță.
- Pentru microfon, căști sau rețea defecte, apelează
  `vesta_report_technical_issue`; o problemă tehnică nu este o dovadă de
  pericol.
- Lasă întotdeauna motorul determinist Python să decidă tranzițiile, retry-urile
  și alerta. Tu asiguri doar conversația și transmiți acțiunile explicite.
""".strip()


@dataclass(frozen=True, slots=True)
class ClientToolSpec:
    """Metadata consumed by the Android/web ElevenLabs client."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


CLIENT_TOOL_SPECS = (
    ClientToolSpec(
        name="vesta_confirm_safe",
        description=(
            "Transmite backendului o confirmare explicită că utilizatoarea "
            "este în regulă; motorul Python validează și aplică tranziția."
        ),
        parameters={
            "type": "object",
            "properties": {
                "checkin_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Identificatorul verificării active primit în "
                        "evenimentul safety.check_in_requested."
                    ),
                },
                "confirmation": {
                    "type": "string",
                    "enum": ["safe"],
                    "description": "Confirmarea explicită a utilizatoarei.",
                },
            },
            "required": ["checkin_id", "confirmation"],
            "additionalProperties": False,
        },
    ),
    ClientToolSpec(
        name="vesta_request_help",
        description=(
            "Transmite o cerere explicită de ajutor către endpointul SOS; "
            "agentul nu decide singur dacă există un pericol."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "Formularea scurtă și explicită a cererii.",
                }
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
    ),
    ClientToolSpec(
        name="vesta_report_technical_issue",
        description=(
            "Raportează separat o problemă audio sau de rețea; nu declanșează "
            "o alertă de siguranță."
        ),
        parameters={
            "type": "object",
            "properties": {
                "issue": {
                    "type": "string",
                    "enum": [
                        "microphone_denied",
                        "audio_interrupted",
                        "audio_disconnected",
                        "voice_timeout",
                        "network_disconnected",
                    ],
                }
            },
            "required": ["issue"],
            "additionalProperties": False,
        },
    ),
)
