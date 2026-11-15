-- 010: compresie columnstore + agregate continue pentru dashboard-ul live (Tiger Data).
--
-- Compresie: chunk-urile vechi trec în columnstore (segmentate pe coloana după care se filtrează),
-- deci istoricul ocupă de obicei sub 10% din spațiul inițial și rămâne interogabil cu SQL simplu.
-- Agregatele continue sunt în timp real (materialized_only = false): includ și ultimele rânduri
-- încă nematerializate, deci dashboard-ul vede imediat un eveniment nou.

-- ---- compresie ----------------------------------------------------------------------------
ALTER TABLE session_events SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'event_type',
    timescaledb.compress_orderby   = 'occurred_at DESC'
);
SELECT add_compression_policy('session_events', compress_after => INTERVAL '7 days');

ALTER TABLE technical_events SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'component',
    timescaledb.compress_orderby   = 'occurred_at DESC'
);
SELECT add_compression_policy('technical_events', compress_after => INTERVAL '2 days');

-- Locațiile se șterg după 7 zile (006); între ziua 1 și 7 stau comprimate.
ALTER TABLE locations SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'source',
    timescaledb.compress_orderby   = 'occurred_at DESC'
);
SELECT add_compression_policy('locations', compress_after => INTERVAL '1 day');

-- ---- sănătatea dispozitivelor IoT, pe oră ----------------------------------------------------
-- Telefon, GPS, alimentare Raspberry Pi, GPIO, audio, serviciul de voce: câte evenimente
-- de fiecare tip, pe componentă. Baza panoului „Sănătate dispozitive”.
CREATE MATERIALIZED VIEW technical_events_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 hour', occurred_at) AS bucket,
       component,
       event_type,
       count(*)                   AS events,
       count(DISTINCT client_id)  AS clients
FROM technical_events
GROUP BY bucket, component, event_type
WITH NO DATA;
ALTER MATERIALIZED VIEW technical_events_hourly SET (timescaledb.materialized_only = false);
SELECT add_continuous_aggregate_policy('technical_events_hourly',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '5 minutes');

-- ---- calitatea semnalului GPS (senzor), la 5 minute -----------------------------------------
-- Fără coordonate: doar numărul de poziții și precizia, pe sursă (GPS dispozitiv / telefon).
CREATE MATERIALIZED VIEW gps_quality_5m
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '5 minutes', occurred_at) AS bucket,
       source,
       count(*)                                           AS fixes,
       avg(accuracy_m)::real                              AS avg_accuracy_m,
       max(accuracy_m)                                    AS worst_accuracy_m,
       count(*) FILTER (WHERE accuracy_m > 100)           AS imprecise_fixes,
       avg(EXTRACT(EPOCH FROM (received_at - occurred_at)))::real AS avg_delay_s
FROM locations
GROUP BY bucket, source
WITH NO DATA;
ALTER MATERIALIZED VIEW gps_quality_5m SET (timescaledb.materialized_only = false);
SELECT add_continuous_aggregate_policy('gps_quality_5m',
    start_offset      => INTERVAL '1 day',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute');
-- agregatul nu ține coordonate, dar respectă aceeași retenție ca sursa
SELECT add_retention_policy('gps_quality_5m', INTERVAL '7 days');
