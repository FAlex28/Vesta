-- Explicitly retired by the team: environmental and proximity sensor history is no longer used.
-- No CASCADE is used. If another object depends on these sensor tables, this migration fails
-- safely and leaves that object and the readings intact for a manual dependency review.
DO $$
DECLARE
    schema_name text := current_schema();
    object_name text;
    object_kind "char";
BEGIN
    FOREACH object_name IN ARRAY ARRAY[
        'legacy_sensor_readings_10s',
        'sensor_readings_10s',
        'legacy_sensor_readings',
        'sensor_readings'
    ] LOOP
        SELECT relation.relkind INTO object_kind
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = schema_name AND relation.relname = object_name;

        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        -- A TimescaleDB continuous aggregate shows up in pg_class as a plain view ('v'),
        -- but must be dropped with DROP MATERIALIZED VIEW.
        IF object_kind = 'm' OR EXISTS (
            SELECT 1 FROM timescaledb_information.continuous_aggregates
            WHERE view_schema = schema_name AND view_name = object_name
        ) THEN
            EXECUTE format('DROP MATERIALIZED VIEW %I.%I', schema_name, object_name);
        ELSIF object_kind = 'v' THEN
            EXECUTE format('DROP VIEW %I.%I', schema_name, object_name);
        ELSE
            EXECUTE format('DROP TABLE %I.%I', schema_name, object_name);
        END IF;
    END LOOP;
END $$;
