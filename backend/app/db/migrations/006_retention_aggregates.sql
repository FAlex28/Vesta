-- 006: funcționalități TimescaleDB folosite acolo unde au un rol real.

-- Coordonatele precise nu se păstrează mai mult decât e nevoie (valoare implicită pentru demo;
-- se poate schimba din cod cu LocationsRepo.set_retention()).
SELECT add_retention_policy('locations', INTERVAL '7 days');
SELECT add_retention_policy('technical_events', INTERVAL '30 days');

-- Statistici zilnice pentru dashboard / Living Labs: activări, anulări, alerte, confirmări.
CREATE MATERIALIZED VIEW session_events_daily
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 day', occurred_at) AS day,
       event_type,
       count(*)                   AS events,
       count(DISTINCT session_id) AS sessions
FROM session_events
GROUP BY day, event_type
WITH NO DATA;

-- agregare în timp real: include și evenimentele încă nematerializate
ALTER MATERIALIZED VIEW session_events_daily SET (timescaledb.materialized_only = false);

SELECT add_continuous_aggregate_policy('session_events_daily',
    start_offset      => INTERVAL '7 days',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '15 minutes');
