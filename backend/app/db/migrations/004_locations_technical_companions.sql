-- 004: locații, evenimente tehnice, starea telefoanelor companion.

CREATE TABLE locations (
    occurred_at TIMESTAMPTZ      NOT NULL,          -- timestamp-ul poziției raportat de telefon
    received_at TIMESTAMPTZ      NOT NULL DEFAULT now(),
    session_id  UUID             REFERENCES sessions (session_id),
    client_id   TEXT,
    lat         DOUBLE PRECISION NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon         DOUBLE PRECISION NOT NULL CHECK (lon BETWEEN -180 AND 180),
    accuracy_m  REAL             CHECK (accuracy_m IS NULL OR accuracy_m >= 0),
    source      TEXT             NOT NULL DEFAULT 'COMPANION_GPS' CHECK (source IN ('COMPANION_GPS', 'DEMO')),
    simulated   BOOLEAN          NOT NULL DEFAULT false,
    CONSTRAINT locations_demo_is_simulated CHECK (simulated = (source = 'DEMO'))
);
SELECT create_hypertable('locations', by_range('occurred_at', INTERVAL '1 day'));
CREATE INDEX locations_session_idx ON locations (session_id, occurred_at DESC);

CREATE TABLE technical_events (
    event_id    UUID        NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    component   TEXT        NOT NULL
        CHECK (component IN ('PHONE', 'PAGE', 'AUDIO', 'VOICE_SERVICE', 'GPS', 'POWER', 'SERVER', 'GPIO')),
    event_type  TEXT        NOT NULL,
    session_id  UUID        REFERENCES sessions (session_id),
    client_id   TEXT,
    metadata    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, occurred_at)
);
SELECT create_hypertable('technical_events', by_range('occurred_at', INTERVAL '7 days'));
CREATE INDEX technical_events_session_idx   ON technical_events (session_id, occurred_at DESC);
CREATE INDEX technical_events_component_idx ON technical_events (component, occurred_at DESC);

-- Starea curentă a fiecărui telefon companion (heartbeaturile nu se salvează individual).
CREATE TABLE companion_clients (
    client_id         TEXT        PRIMARY KEY,
    owner_id          TEXT        NOT NULL,
    session_id        UUID        REFERENCES sessions (session_id),
    connection_state  TEXT        NOT NULL DEFAULT 'CONNECTED'
        CHECK (connection_state IN ('CONNECTED', 'RECONNECTING', 'DISCONNECTED')),
    visibility        TEXT        NOT NULL DEFAULT 'UNKNOWN' CHECK (visibility IN ('VISIBLE', 'HIDDEN', 'UNKNOWN')),
    last_heartbeat_at TIMESTAMPTZ NOT NULL,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_reconnect_at TIMESTAMPTZ,
    disconnected_at   TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX companion_clients_owner_idx ON companion_clients (owner_id);
