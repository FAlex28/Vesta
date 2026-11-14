"""Conexiunea la Tiger Data (PostgreSQL + TimescaleDB) prin pg8000.

pg8000 e scris doar în Python: merge identic pe Windows (unde DLL-urile psycopg sunt
blocate de Application Control) și pe Raspberry Pi.
"""

from __future__ import annotations

import os
import queue
import re
import ssl
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlparse

import pg8000.dbapi
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

DisconnectErrors = (pg8000.dbapi.InterfaceError, OSError)


def load_dsn() -> str:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    dsn = os.environ.get("TIMESCALE_SERVICE_URL")
    if not dsn:
        raise RuntimeError("TIMESCALE_SERVICE_URL lipsește. Pune-l în .env.")
    return dsn


def connect_kwargs(dsn: str) -> dict:
    u = urlparse(dsn)
    q = parse_qs(u.query)
    kw: dict[str, Any] = {
        "host": u.hostname, "port": u.port or 5432,
        "user": unquote(u.username or ""), "password": unquote(u.password or ""),
        "database": (u.path or "/tsdb").lstrip("/") or "tsdb",
        "application_name": "vesta", "timeout": 15, "tcp_keepalive": True,
    }
    if q.get("sslmode", ["require"])[0] != "disable":
        kw["ssl_context"] = ssl.create_default_context()
    return kw


def rows_as_dicts(cur) -> list[dict]:
    if cur.description is None:
        return []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class Tx:
    """Cursor cu rezultate ca dict. Folosit atât în tranzacții, cât și în autocommit."""

    def __init__(self, cur):
        self._cur = cur

    def all(self, sql: str, params: Any = None) -> list[dict]:
        if params is None:
            self._cur.execute(sql)
        else:
            self._cur.execute(sql, params)
        return rows_as_dicts(self._cur)

    def one(self, sql: str, params: Any = None) -> dict | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def exec(self, sql: str, params: Any = None) -> int:
        self.all(sql, params)
        return self._cur.rowcount


class ConnectionPool:
    """Pool mic, thread-safe. Conexiunile căzute (Wi-Fi, idle timeout) se aruncă și se redeschid."""

    def __init__(self, dsn: str, *, max_size: int = 4, search_path: str | None = None):
        if search_path is not None and not _IDENT.match(search_path):
            raise ValueError(f"search_path invalid: {search_path!r}")
        self._kw = connect_kwargs(dsn)
        self._search_path = search_path
        self._pool: queue.LifoQueue = queue.LifoQueue(maxsize=max_size)
        for _ in range(max_size):
            self._pool.put(None)

    def _new_conn(self):
        conn = pg8000.dbapi.connect(**self._kw)
        conn.autocommit = True
        if self._search_path:
            conn.execute_simple(f"SET search_path TO {self._search_path}, public")
        return conn

    @contextmanager
    def connection(self) -> Iterator[Any]:
        conn = self._pool.get(timeout=30)
        try:
            if conn is None:
                conn = self._new_conn()
            yield conn
        except DisconnectErrors:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None
            raise
        finally:
            self._pool.put(conn)

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        with self.connection() as conn:
            cur = conn.cursor()
            cur.execute("BEGIN")
            try:
                yield Tx(cur)
            except BaseException:
                try:
                    cur.execute("ROLLBACK")
                except DisconnectErrors:
                    raise
                except Exception:
                    pass
                raise
            else:
                cur.execute("COMMIT")

    @contextmanager
    def autocommit(self) -> Iterator[Tx]:
        with self.connection() as conn:
            yield Tx(conn.cursor())

    def close(self) -> None:
        while not self._pool.empty():
            conn = self._pool.get_nowait()
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
