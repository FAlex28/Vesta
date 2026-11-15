-- 011: o frază pe care asistentul nu a putut-o transcrie (fără cheie Groq sau eroare de rețea)
-- se salvează cu action = 'TRANSCRIPTION_UNAVAILABLE'. Fără valoarea asta în CHECK, rândul era
-- respins și fraza lipsea din transcrierea din Tiger Data.

ALTER TABLE conversation_turns DROP CONSTRAINT IF EXISTS conversation_turns_action_check;
ALTER TABLE conversation_turns ADD CONSTRAINT conversation_turns_action_check
    CHECK (action IS NULL OR action IN
           ('NONE', 'FALSE_ALARM', 'ESCALATE', 'CONFIRM_SAFE', 'TRANSCRIPTION_UNAVAILABLE'));
