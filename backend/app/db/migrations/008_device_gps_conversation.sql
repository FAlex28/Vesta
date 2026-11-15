-- 008: GPS-ul dispozitivului Vesta ca sursă de locație și conversația cu asistentul din căști.
-- La o alarmă falsă, pozițiile și conversația sesiunii se șterg (vezi purge în integrations/runtime).

ALTER TABLE locations DROP CONSTRAINT IF EXISTS locations_source_check;
ALTER TABLE locations ADD CONSTRAINT locations_source_check
    CHECK (source IN ('COMPANION_GPS', 'DEVICE_GPS', 'DEMO'));

CREATE TABLE IF NOT EXISTS conversation_turns (
    turn_id     UUID        PRIMARY KEY,
    -- fără cheie străină: fraza poate ajunge înaintea rândului sesiunii (scrieri asincrone)
    session_id  UUID        NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    role        TEXT        NOT NULL CHECK (role IN ('PERSON', 'ASSISTANT')),
    text        TEXT        NOT NULL,
    action      TEXT        CHECK (action IS NULL OR action IN ('NONE', 'FALSE_ALARM', 'ESCALATE', 'CONFIRM_SAFE'))
);
CREATE INDEX IF NOT EXISTS conversation_turns_session_idx
    ON conversation_turns (session_id, occurred_at);
