"""Analize live din Tiger Data: agregate continue, metrici în timp real și starea compresiei.

Toate interogările sunt SQL simplu peste tabelele relaționale (sessions, alerts, outbox) și
peste hypertable-urile / agregatele continue (session_events_daily, technical_events_hourly,
gps_quality_5m). Nu se întorc coordonate.
"""

from __future__ import annotations

from .config import DbSettings
from .pool import ConnectionPool

HYPERTABLES = ("session_events", "technical_events", "locations")


class AnalyticsRepo:
    def __init__(self, pool: ConnectionPool, settings: DbSettings):
        self._pool = pool
        self._s = settings

    def live(self) -> dict:
        """Starea operațională curentă (tabele relaționale, fără agregare)."""
        with self._pool.autocommit() as tx:
            row = tx.one(
                """
                SELECT
                  (SELECT count(*) FROM sessions
                     WHERE state IN ('ACTIVE','CHECK_IN','UNCONFIRMED','ALERT'))          AS open_sessions,
                  (SELECT count(*) FROM alerts WHERE status = 'ACTIVE')                    AS active_alerts,
                  (SELECT count(*) FROM notification_attempts
                     WHERE status IN ('PENDING','SENDING','FAILED'))                        AS notifications_pending,
                  (SELECT count(*) FROM companion_clients
                     WHERE connection_state = 'CONNECTED')                                 AS phones_connected,
                  (SELECT count(*) FROM companion_clients
                     WHERE connection_state <> 'CONNECTED')                                AS phones_degraded,
                  (SELECT max(occurred_at) FROM locations)                                 AS last_gps_fix_at,
                  now()                                                                    AS server_time
                """
            )
        return row or {}

    def alert_response(self, days: int = 7) -> dict:
        """Cât durează de la ridicarea alertei până când contactul primește / vede notificarea."""
        with self._pool.autocommit() as tx:
            row = tx.one(
                """
                WITH n AS (
                  SELECT EXTRACT(EPOCH FROM (n.delivered_at - a.created_at)) AS to_delivered_s,
                         EXTRACT(EPOCH FROM (n.viewed_at    - a.created_at)) AS to_viewed_s,
                         n.status
                  FROM notification_attempts n JOIN alerts a USING (alert_id)
                  WHERE n.kind = 'ALERT_RAISED'
                    AND a.created_at > now() - make_interval(days => %s::int)
                )
                SELECT count(*)                                                        AS notifications,
                       count(to_delivered_s)                                           AS delivered,
                       count(to_viewed_s)                                              AS viewed,
                       count(*) FILTER (WHERE status = 'GAVE_UP')                      AS gave_up,
                       percentile_cont(0.5)  WITHIN GROUP (ORDER BY to_delivered_s)    AS p50_delivered_s,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY to_delivered_s)    AS p95_delivered_s,
                       percentile_cont(0.5)  WITHIN GROUP (ORDER BY to_viewed_s)       AS p50_viewed_s
                FROM n
                """,
                (days,),
            )
        return row or {}

    def daily(self, days: int = 7) -> list[dict]:
        with self._pool.autocommit() as tx:
            return tx.all(
                "SELECT day, event_type, events, sessions FROM session_events_daily "
                "WHERE day > now() - make_interval(days => %s::int) ORDER BY day, event_type",
                (days,),
            )

    def device_health(self, hours: int = 24) -> list[dict]:
        with self._pool.autocommit() as tx:
            return tx.all(
                "SELECT bucket, component, event_type, events, clients FROM technical_events_hourly "
                "WHERE bucket > now() - make_interval(hours => %s::int) ORDER BY bucket, component, event_type",
                (hours,),
            )

    def gps_quality(self, hours: int = 6) -> list[dict]:
        with self._pool.autocommit() as tx:
            return tx.all(
                "SELECT bucket, source, fixes, avg_accuracy_m, worst_accuracy_m, imprecise_fixes, avg_delay_s "
                "FROM gps_quality_5m WHERE bucket > now() - make_interval(hours => %s::int) "
                "ORDER BY bucket, source",
                (hours,),
            )

    def storage(self) -> list[dict]:
        """Mărimea fiecărui hypertable și cât a câștigat compresia columnstore."""
        with self._pool.autocommit() as tx:
            return tx.all(
                """
                SELECT h.hypertable_name                                    AS name,
                       h.num_chunks                                         AS chunks,
                       hypertable_size((quote_ident(h.hypertable_schema) || '.' || quote_ident(h.hypertable_name))::regclass)
                                                                            AS total_bytes,
                       c.total_chunks                                       AS compressible_chunks,
                       c.number_compressed_chunks                           AS compressed_chunks,
                       c.before_compression_total_bytes                     AS before_bytes,
                       c.after_compression_total_bytes                      AS after_bytes
                FROM timescaledb_information.hypertables h
                LEFT JOIN LATERAL hypertable_compression_stats(
                    (quote_ident(h.hypertable_schema) || '.' || quote_ident(h.hypertable_name))::regclass) c ON true
                WHERE h.hypertable_schema = current_schema()
                  AND h.hypertable_name = ANY(%s)
                ORDER BY h.hypertable_name
                """,
                (list(HYPERTABLES),),
            )

    def snapshot(self, *, days: int = 7, hours: int = 24) -> dict:
        return {
            "live": self.live(),
            "alert_response": self.alert_response(days),
            "daily": self.daily(days),
            "device_health": self.device_health(hours),
            "gps_quality": self.gps_quality(min(hours, 24)),
            "storage": self.storage(),
        }
