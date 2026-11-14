-- 002: sesiuni + istoricul evenimentelor de sesiune + deduplicare globală după event_id.

-- Fiecare event_id acceptat o singură dată, indiferent de tabelul în care ajunge.
-- (Pe un hypertable, o cheie unică trebuie să includă coloana de timp; tabelul acesta
--  garantează unicitatea doar după event_id, chiar dacă un client retrimite alt occurred_at.)
CREATE TABLE ingested_events (
    event_id     UUID        PRIMARY KEY,
    kind         TEXT        NOT NULL,           -- SESSION | TECHNICAL | ACTIVATION | ALERT ...
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    session_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id            TEXT        NOT NULL,     -- utilizatoarea / dispozitivul Vesta
    activation_event_id UUID        NOT NULL UNIQUE,
    state               TEXT        NOT NULL DEFAULT 'ACTIVE'
        CHECK (state IN ('ACTIVE', 'CHECK_IN', 'UNCONFIRMED', 'ALERT', 'CANCELLED', 'INTERRUPTED')),
    state_version       INT         NOT NULL DEFAULT 1,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancel_window_until TIMESTAMPTZ,
    ended_at            TIMESTAMPTZ,
    end_reason          TEXT,                     -- ACCIDENTAL_ACTIVATION, USER_ENDED, ALERT_RESOLVED, SERVER_RESTART ...
    simulated           BOOLEAN     NOT NULL DEFAULT false,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sessions_ended_consistent
        CHECK ((state IN ('CANCELLED', 'INTERRUPTED')) = (ended_at IS NOT NULL))
);

-- O singură sesiune deschisă per persoană; sesiunile încheiate nu sunt afectate.
CREATE UNIQUE INDEX sessions_one_open_per_owner
    ON sessions (owner_id) WHERE state IN ('ACTIVE', 'CHECK_IN', 'UNCONFIRMED', 'ALERT');
CREATE INDEX sessions_owner_started_idx ON sessions (owner_id, started_at DESC);

-- Plasă de siguranță: o sesiune încheiată nu se mai redeschide; versiunea crește la fiecare schimbare.
CREATE FUNCTION sessions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state IN ('CANCELLED', 'INTERRUPTED') AND NEW.state <> OLD.state THEN
        RAISE EXCEPTION 'session % is % and cannot change state', OLD.session_id, OLD.state
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.state <> OLD.state THEN
        NEW.state_version := OLD.state_version + 1;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;
CREATE TRIGGER sessions_guard BEFORE UPDATE ON sessions FOR EACH ROW EXECUTE FUNCTION sessions_guard();

CREATE TABLE session_events (
    event_id        UUID        NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    session_id      UUID        REFERENCES sessions (session_id),
    event_type      TEXT        NOT NULL,
    source          TEXT        NOT NULL
        CHECK (source IN ('GPIO', 'COMPANION', 'DASHBOARD', 'CONTACT', 'SAFETY_ENGINE', 'VOICE', 'SYSTEM')),
    sequence_number INT,                          -- state_version după eveniment, dacă e cazul
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, occurred_at)
);
SELECT create_hypertable('session_events', by_range('occurred_at', INTERVAL '7 days'));
CREATE INDEX session_events_session_idx ON session_events (session_id, occurred_at DESC);
CREATE INDEX session_events_type_idx    ON session_events (event_type, occurred_at DESC);
