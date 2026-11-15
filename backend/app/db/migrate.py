"""Rulează migrările SQL numerotate din migrations/ (o singură dată fiecare, în tranzacție).

cd backend && python -m app.db.migrate            # aplică ce lipsește
cd backend && python -m app.db.migrate --status   # doar afișează
"""

from __future__ import annotations

import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_LOCK_ID = 7_270_001  # pg_advisory_lock: două procese nu migrează simultan


def _files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def status(pool) -> dict[str, bool]:
    with pool.connection() as conn:
        conn.execute_simple(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur = conn.cursor()
        cur.execute("SELECT version FROM schema_migrations")
        applied = {r[0] for r in cur.fetchall()}
    return {f.stem: f.stem in applied for f in _files()}


def migrate(pool) -> list[str]:
    done: list[str] = []
    pending = [v for v, ok in status(pool).items() if not ok]
    if not pending:
        return done
    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
        try:
            cur.execute("SELECT version FROM schema_migrations")
            applied = {r[0] for r in cur.fetchall()}
            for f in _files():
                if f.stem in applied:
                    continue
                body = f.read_text(encoding="utf-8")
                try:
                    conn.execute_simple(
                        "BEGIN;\n"
                        + body
                        + f"\n;INSERT INTO schema_migrations (version) VALUES ('{f.stem}');\nCOMMIT;"
                    )
                except Exception:
                    conn.execute_simple("ROLLBACK")
                    raise
                done.append(f.stem)
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
    return done


if __name__ == "__main__":
    from .pool import ConnectionPool, load_dsn

    p = ConnectionPool(load_dsn(), max_size=1)
    try:
        if "--status" in sys.argv:
            for v, ok in status(p).items():
                print(f"{'[x]' if ok else '[ ]'} {v}")
        else:
            applied = migrate(p)
            print("Aplicate: " + (", ".join(applied) if applied else "nimic nou"))
    finally:
        p.close()
