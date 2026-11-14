-- 001: păstrează tabelele din varianta cu senzori (ESP32) sub prefixul legacy_.
-- Nu șterge nimic. Acționează doar în schema curentă și doar dacă tabelele vechi există.

CREATE EXTENSION IF NOT EXISTS timescaledb;

DO $$
DECLARE
    s   text := current_schema();
    t   text;
    idx record;
BEGIN
    -- vechiul tabel `sessions` are coloana `id` BIGINT; cel nou are `session_id` UUID
    IF to_regclass(format('%I.sessions', s)) IS NULL
       OR NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_schema = s AND table_name = 'sessions' AND column_name = 'device_id') THEN
        RETURN;
    END IF;

    IF to_regclass(format('%I.sensor_readings_10s', s)) IS NOT NULL THEN
        EXECUTE format('ALTER MATERIALIZED VIEW %I.sensor_readings_10s RENAME TO legacy_sensor_readings_10s', s);
    END IF;

    FOREACH t IN ARRAY ARRAY['sensor_readings', 'locations', 'events', 'sessions'] LOOP
        IF to_regclass(format('%I.%I', s, t)) IS NOT NULL THEN
            -- numele indexurilor sunt unice pe schemă → le redenumim ca să nu se ciocnească cu cele noi
            FOR idx IN SELECT indexname FROM pg_indexes
                       WHERE schemaname = s AND tablename = t AND indexname NOT LIKE 'legacy\_%' LOOP
                EXECUTE format('ALTER INDEX %I.%I RENAME TO %I', s, idx.indexname, 'legacy_' || idx.indexname);
            END LOOP;
            EXECUTE format('ALTER TABLE %I.%I RENAME TO %I', s, t, 'legacy_' || t);
        END IF;
    END LOOP;

    IF to_regclass(format('%I.sessions_id_seq', s)) IS NOT NULL THEN
        EXECUTE format('ALTER SEQUENCE %I.sessions_id_seq RENAME TO legacy_sessions_id_seq', s);
    END IF;
END $$;
