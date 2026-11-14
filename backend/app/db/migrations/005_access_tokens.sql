-- 005: tokenuri de acces cu durată limitată. Se stochează doar hash-ul SHA-256, niciodată tokenul.

CREATE TABLE access_tokens (
    token_hash   BYTEA       PRIMARY KEY,
    -- USER: utilizatoarea, control pe sesiunile proprii
    -- CONTACT: contactul de încredere, doar citire pe o singură sesiune
    -- OPERATOR: dashboard privat al echipei, citire pe sesiunile unui owner
    -- PUBLIC_DEMO: dashboard de prezentare, doar sesiuni simulate
    role         TEXT        NOT NULL CHECK (role IN ('USER', 'CONTACT', 'OPERATOR', 'PUBLIC_DEMO')),
    owner_id     TEXT        NOT NULL,
    session_id   UUID        REFERENCES sessions (session_id),
    label        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    CONSTRAINT access_tokens_contact_scoped CHECK (role <> 'CONTACT' OR session_id IS NOT NULL)
);
CREATE INDEX access_tokens_owner_idx ON access_tokens (owner_id);
