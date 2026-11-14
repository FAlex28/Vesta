"""Testele rulează pe Tiger Data, într-o schemă PostgreSQL temporară (vesta_test_<random>),
ștearsă la final. Datele reale din schema public nu sunt atinse."""

from __future__ import annotations

import os
import uuid

import pytest

from app.db import Database, DbSettings
from app.db.pool import ConnectionPool, load_dsn

pytestmark = pytest.mark.integration


def _dsn_or_skip() -> str:
    try:
        return load_dsn()
    except RuntimeError:
        pytest.skip("TIMESCALE_SERVICE_URL lipsește")


@pytest.fixture(scope="session")
def dsn() -> str:
    return _dsn_or_skip()


def _make_db(dsn: str) -> tuple[Database, str]:
    schema = f"vesta_test_{uuid.uuid4().hex[:10]}"
    admin = ConnectionPool(dsn, max_size=1)
    with admin.connection() as conn:
        conn.execute_simple(f"CREATE SCHEMA {schema}")
    admin.close()
    db = Database(dsn, search_path=schema, max_size=6,
                  settings=DbSettings(cancel_window_s=5, checkin_timeout_s=15, heartbeat_interval_s=15,
                                      heartbeat_disconnect_s=45, gps_stale_s=60, gps_low_accuracy_m=100,
                                      notify_max_attempts=3, notify_backoff_s=0, token_ttl_minutes=60))
    db.migrate()
    return db, schema


def _drop(dsn: str, db: Database, schema: str) -> None:
    db.close()
    if os.environ.get("VESTA_KEEP_TEST_SCHEMA"):
        return
    admin = ConnectionPool(dsn, max_size=1)
    with admin.connection() as conn:
        conn.execute_simple(f"DROP SCHEMA {schema} CASCADE")
    admin.close()


@pytest.fixture(scope="session")
def db(dsn):
    """Schemă comună pentru majoritatea testelor; fiecare test folosește alt owner_id."""
    d, schema = _make_db(dsn)
    yield d
    _drop(dsn, d, schema)


@pytest.fixture
def fresh_db(dsn):
    """Schemă proprie, pentru teste care ating toate rândurile (ex. restart)."""
    d, schema = _make_db(dsn)
    yield d
    _drop(dsn, d, schema)


@pytest.fixture
def owner() -> str:
    return f"owner-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def ev():
    return lambda: uuid.uuid4()


@pytest.fixture
def active_session(db, owner, ev):
    return db.sessions.activate(owner, ev()).session
