"""Stratul de date Vesta (Tiger Data / TimescaleDB).

from app.db import Database
db = Database()        # citește TIMESCALE_SERVICE_URL din .env
db.migrate()           # aplică migrările lipsă (idempotent)
r = db.sessions.activate("owner-1", event_id)
"""

from __future__ import annotations

from .alerts import AlertsRepo
from .analytics import AnalyticsRepo
from .checkins import CheckinsRepo
from .companions import CompanionsRepo
from .config import DbSettings
from .conversations import ConversationsRepo
from .errors import *  # noqa: F401,F403
from .events import EventsRepo, derived_event_id
from .locations import LocationsRepo
from .migrate import migrate as _migrate
from .migrate import status as _status
from .notifications import NotificationsRepo
from .pool import ConnectionPool, load_dsn
from .recordings import RecordingsRepo
from .sessions import SessionsRepo
from .tokens import TokensRepo


class Database:
    def __init__(
        self,
        dsn: str | None = None,
        *,
        search_path: str | None = None,
        max_size: int = 4,
        settings: DbSettings | None = None,
    ):
        self.settings = settings or DbSettings.from_env()
        self.pool = ConnectionPool(dsn or load_dsn(), max_size=max_size, search_path=search_path)
        s = self.settings
        self.sessions = SessionsRepo(self.pool, s)
        self.checkins = CheckinsRepo(self.pool, s)
        self.alerts = AlertsRepo(self.pool, s)
        self.notifications = NotificationsRepo(self.pool, s)
        self.locations = LocationsRepo(self.pool, s)
        self.companions = CompanionsRepo(self.pool, s)
        self.conversations = ConversationsRepo(self.pool, s)
        self.recordings = RecordingsRepo(self.pool, s)
        self.tokens = TokensRepo(self.pool, s)
        self.events = EventsRepo(self.pool)
        self.analytics = AnalyticsRepo(self.pool, s)

    def migrate(self) -> list[str]:
        return _migrate(self.pool)

    def migration_status(self) -> dict[str, bool]:
        return _status(self.pool)

    def ping(self) -> dict:
        with self.pool.autocommit() as tx:
            return tx.one(
                "SELECT now() AS server_time, current_schema() AS schema, "
                "(SELECT extversion FROM pg_extension WHERE extname = 'timescaledb') AS timescaledb"
            )

    def close(self) -> None:
        self.pool.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["Database", "DbSettings", "derived_event_id"]
