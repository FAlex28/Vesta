-- 009: înregistrările convorbirilor (audio/webm în bucăți), detaliile alertei și mesajele vocale
-- ale dispecerului. Transcrierea este în conversation_turns (008). O alarmă falsă le șterge.

CREATE TABLE IF NOT EXISTS session_recordings (
    session_id   UUID        PRIMARY KEY,
    device_id    TEXT,
    mime_type    TEXT        NOT NULL DEFAULT 'audio/webm',
    chunks       INTEGER     NOT NULL DEFAULT 0,
    size_bytes   BIGINT      NOT NULL DEFAULT 0,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalized    BOOLEAN     NOT NULL DEFAULT false,
    alert_id     TEXT,
    alert_reason TEXT,
    alert_at     TIMESTAMPTZ,
    resolution   TEXT,
    resolved_at  TIMESTAMPTZ,
    operator_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS session_recordings_alert_idx ON session_recordings (alert_at DESC);

CREATE TABLE IF NOT EXISTS recording_chunks (
    session_id UUID    NOT NULL,
    seq        INTEGER NOT NULL,
    data       BYTEA   NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS operator_messages (
    message_id UUID        PRIMARY KEY,
    session_id UUID        NOT NULL,
    at         TIMESTAMPTZ NOT NULL,
    mime_type  TEXT        NOT NULL,
    data       BYTEA       NOT NULL
);
CREATE INDEX IF NOT EXISTS operator_messages_session_idx ON operator_messages (session_id, at);

-- Mesajele dispecerului apar și ele în transcriere.
ALTER TABLE conversation_turns DROP CONSTRAINT IF EXISTS conversation_turns_role_check;
ALTER TABLE conversation_turns ADD CONSTRAINT conversation_turns_role_check
    CHECK (role IN ('PERSON', 'ASSISTANT', 'OPERATOR'));
