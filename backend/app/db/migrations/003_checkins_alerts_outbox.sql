-- 003: verificări de siguranță, alerte, outbox de notificări.

CREATE TABLE safety_checkins (
    checkin_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id       UUID        NOT NULL REFERENCES sessions (session_id),
    round_no         INT         NOT NULL CHECK (round_no >= 1),
    attempt_no       INT         NOT NULL CHECK (attempt_no >= 1),
    -- REQUESTED: generată, nelivrată | DELIVERED: livrată pe un canal | AUDIO_UNAVAILABLE: audio căzut,
    -- se așteaptă confirmare din UI | CONFIRMED | TIMED_OUT | CANCELLED (sesiune închisă / restart)
    status           TEXT        NOT NULL DEFAULT 'REQUESTED'
        CHECK (status IN ('REQUESTED', 'DELIVERED', 'AUDIO_UNAVAILABLE', 'CONFIRMED', 'TIMED_OUT', 'CANCELLED')),
    delivery_channel TEXT        CHECK (delivery_channel IN ('AUDIO', 'COMPANION_UI')),
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at     TIMESTAMPTZ,
    expires_at       TIMESTAMPTZ NOT NULL,
    confirmed_at     TIMESTAMPTZ,
    confirmed_via    TEXT        CHECK (confirmed_via IN ('VOICE_TOOL', 'UI_BUTTON')),
    closed_at        TIMESTAMPTZ,
    UNIQUE (session_id, round_no, attempt_no)
);
-- cel mult o verificare deschisă per sesiune
CREATE UNIQUE INDEX safety_checkins_one_open
    ON safety_checkins (session_id) WHERE status IN ('REQUESTED', 'DELIVERED', 'AUDIO_UNAVAILABLE');
CREATE INDEX safety_checkins_due_idx
    ON safety_checkins (expires_at) WHERE status IN ('REQUESTED', 'DELIVERED', 'AUDIO_UNAVAILABLE');

CREATE TABLE alerts (
    alert_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        UUID        NOT NULL REFERENCES sessions (session_id),
    incident_type     TEXT        NOT NULL
        CHECK (incident_type IN ('SOS_EXPLICIT', 'CHECKIN_TIMEOUT', 'PHONE_DISCONNECTED')),
    triggers          TEXT[]      NOT NULL DEFAULT '{}',   -- toate motivele care au dus la aceeași alertă
    status            TEXT        NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'RESOLVED')),
    version           INT         NOT NULL DEFAULT 1,
    location_snapshot JSONB,                               -- ultima locație autorizată + vechimea ei
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMPTZ,
    resolved_by       TEXT,
    resolution_reason TEXT,
    CONSTRAINT alerts_resolved_consistent CHECK ((status = 'RESOLVED') = (resolved_at IS NOT NULL))
);
-- o singură alertă activă per sesiune (SOS + timeout simultan → aceeași alertă)
CREATE UNIQUE INDEX alerts_one_active_per_session ON alerts (session_id) WHERE status = 'ACTIVE';
CREATE INDEX alerts_created_idx ON alerts (created_at DESC);

CREATE FUNCTION alerts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'RESOLVED' AND NEW.status <> 'RESOLVED' THEN
        RAISE EXCEPTION 'alert % is RESOLVED and cannot be reopened', OLD.alert_id
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.version := OLD.version + 1;
    NEW.updated_at := now();
    RETURN NEW;
END $$;
CREATE TRIGGER alerts_guard BEFORE UPDATE ON alerts FOR EACH ROW EXECUTE FUNCTION alerts_guard();

-- Outbox: o comandă de notificare = un rând; retrimiterile se fac pe același rând.
CREATE TABLE notification_attempts (
    notification_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id        UUID        NOT NULL REFERENCES alerts (alert_id),
    kind            TEXT        NOT NULL CHECK (kind IN ('ALERT_RAISED', 'ALERT_RESOLVED')),
    channel         TEXT        NOT NULL DEFAULT 'CONTACT_DEMO_PAGE',
    recipient_id    TEXT        NOT NULL,
    -- PENDING → SENDING → SENT (canalul a acceptat) → DELIVERED (pagina a confirmat primirea) → VIEWED
    -- FAILED = se va reîncerca; GAVE_UP = s-a atins max_attempts
    status          TEXT        NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'DELIVERED', 'VIEWED', 'FAILED', 'GAVE_UP')),
    attempt_count   INT         NOT NULL DEFAULT 0,
    max_attempts    INT         NOT NULL DEFAULT 5,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error      TEXT,
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ,
    delivered_at    TIMESTAMPTZ,
    viewed_at       TIMESTAMPTZ,
    UNIQUE (alert_id, kind, channel, recipient_id)
);
CREATE INDEX notification_attempts_due_idx
    ON notification_attempts (next_attempt_at) WHERE status IN ('PENDING', 'FAILED');
