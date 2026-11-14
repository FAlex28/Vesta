"""Outbox pentru notificări către contactul de încredere.

SENT      = canalul (ex. WebSocket către pagina contactului) a acceptat mesajul.
DELIVERED = pagina contactului a confirmat primirea (ack).
VIEWED    = pagina contactului a raportat că notificarea a fost afișată.
Existența unui rând în baza de date NU înseamnă că mesajul a ajuns la contact."""

from __future__ import annotations

from .events import as_uuid

_MAX_BACKOFF_S = 300


class NotificationsRepo:
    def __init__(self, pool, settings):
        self._pool = pool
        self._s = settings

    def claim_due(self, limit: int = 10) -> list[dict]:
        """Rezervă notificările scadente (sigur și cu mai mulți workeri)."""
        with self._pool.transaction() as tx:
            return tx.all(
                """
                UPDATE notification_attempts n
                SET status = 'SENDING', attempt_count = n.attempt_count + 1, updated_at = now()
                WHERE n.notification_id IN (
                    SELECT notification_id FROM notification_attempts
                    WHERE status IN ('PENDING', 'FAILED') AND next_attempt_at <= now()
                    ORDER BY next_attempt_at LIMIT %s FOR UPDATE SKIP LOCKED)
                RETURNING n.*
                """,
                (limit,),
            )

    def mark_sent(self, notification_id) -> dict | None:
        with self._pool.autocommit() as tx:
            return tx.one(
                "UPDATE notification_attempts SET status = 'SENT', sent_at = now(), last_error = NULL, "
                "updated_at = now() WHERE notification_id = %s AND status = 'SENDING' RETURNING *",
                (as_uuid(notification_id),),
            )

    def mark_failed(self, notification_id, error: str) -> dict | None:
        """Reîncercare cu interval progresiv; după max_attempts → GAVE_UP (nu se retrimite la nesfârșit)."""
        with self._pool.autocommit() as tx:
            return tx.one(
                """
                UPDATE notification_attempts SET
                    status = CASE WHEN attempt_count >= max_attempts THEN 'GAVE_UP' ELSE 'FAILED' END,
                    last_error = left(%s, 500), updated_at = now(),
                    next_attempt_at = now() + make_interval(secs => LEAST(
                        %s::double precision * power(2, GREATEST(attempt_count - 1, 0)), %s::double precision))
                WHERE notification_id = %s AND status = 'SENDING' RETURNING *
                """,
                (error, self._s.notify_backoff_s, _MAX_BACKOFF_S, as_uuid(notification_id)),
            )

    def mark_delivered(self, notification_id) -> dict | None:
        """Ack de la pagina contactului. Nu retrogradează o notificare deja VIEWED."""
        with self._pool.autocommit() as tx:
            return tx.one(
                "UPDATE notification_attempts SET status = 'DELIVERED', "
                "delivered_at = COALESCE(delivered_at, now()), updated_at = now() "
                "WHERE notification_id = %s AND status IN ('SENDING', 'SENT', 'FAILED', 'GAVE_UP') RETURNING *",
                (as_uuid(notification_id),),
            )

    def mark_viewed(self, notification_id) -> dict | None:
        with self._pool.autocommit() as tx:
            return tx.one(
                "UPDATE notification_attempts SET status = 'VIEWED', viewed_at = COALESCE(viewed_at, now()), "
                "delivered_at = COALESCE(delivered_at, now()), updated_at = now() "
                "WHERE notification_id = %s AND status <> 'PENDING' RETURNING *",
                (as_uuid(notification_id),),
            )

    def for_alert(self, alert_id) -> list[dict]:
        with self._pool.autocommit() as tx:
            return tx.all("SELECT * FROM notification_attempts WHERE alert_id = %s ORDER BY created_at",
                          (as_uuid(alert_id),))

    def for_recipient(self, recipient_id: str, limit: int = 50) -> list[dict]:
        """Pentru pagina contactului la (re)conectare: tot ce i s-a trimis, cu starea curentă."""
        with self._pool.autocommit() as tx:
            return tx.all(
                "SELECT n.*, a.status AS alert_status, a.resolved_at FROM notification_attempts n "
                "JOIN alerts a USING (alert_id) WHERE n.recipient_id = %s ORDER BY n.created_at DESC LIMIT %s",
                (recipient_id, limit),
            )
