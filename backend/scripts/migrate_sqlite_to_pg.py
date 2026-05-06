"""One-shot data migration: copy every row from the legacy SQLite DB into the
new Postgres backend.

Usage (from the backend container, with DATABASE_URL set):

    python scripts/migrate_sqlite_to_pg.py /app/data/extraction_history.db

Run AFTER `alembic upgrade head` (so target tables exist) but BEFORE you start
serving traffic from Postgres. Idempotent: rows are inserted with
`ON CONFLICT DO NOTHING`, so re-runs are safe.
"""

from __future__ import annotations

import os
import sys
import sqlite3
from pathlib import Path
from typing import Iterable

# Make `backend/` importable so we can reuse db_engine.
HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))

import db_engine  # noqa: E402


# Order matters for FK-like ordering (we don't have hard FKs but it keeps logs
# sane). Tables not listed here are skipped silently.
TABLES = [
    "users",
    "groups",
    "user_groups",
    "ldap_configs",
    "storage_config",
    "settings",
    "jobs",
    "items",
    "declarations",
    "pdf_metadata",
    "processing_logs",
    "page_extractions",
    "page_contents",
    "importer_profiles",
    "field_accuracy",
    "value_audit",
    "corrections",
    "learning_events",
    "activity_logs",
    "field_edits",
]


def _table_exists_sqlite(src: sqlite3.Connection, name: str) -> bool:
    cur = src.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def _pg_columns(dst, table: str) -> list[str]:
    cur = dst.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def _coerce(val):
    """SQLite stores everything loosely; just pass through (psycopg adapts)."""
    return val


def migrate_table(src: sqlite3.Connection, dst, table: str) -> int:
    if not _table_exists_sqlite(src, table):
        print(f"  [skip] {table}: not in source DB")
        return 0

    src.row_factory = sqlite3.Row
    rows = src.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        print(f"  [empty] {table}")
        return 0

    pg_cols = set(_pg_columns(dst, table))
    if not pg_cols:
        print(f"  [skip] {table}: not in target DB")
        return 0

    src_cols = list(rows[0].keys())
    common = [c for c in src_cols if c in pg_cols]
    if not common:
        print(f"  [skip] {table}: no overlapping columns")
        return 0

    placeholders = ", ".join(["%s"] * len(common))
    col_list = ", ".join(common)
    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT DO NOTHING"
    )

    cur = dst.cursor()
    inserted = 0
    for row in rows:
        try:
            cur.execute(sql, tuple(_coerce(row[c]) for c in common))
            inserted += cur.rowcount or 0
        except Exception as exc:
            print(f"  [warn] {table} row {row.get('id', '?')}: {exc}")
            dst.rollback()
            continue
    dst.commit()
    print(f"  [ok]   {table}: {inserted}/{len(rows)} rows inserted")
    return inserted


def reset_serial_sequences(dst, tables: Iterable[str]) -> None:
    """Bump SERIAL/IDENTITY sequences past the largest imported id so future
    inserts don't collide with imported PKs."""
    cur = dst.cursor()
    for tbl in tables:
        try:
            cur.execute(
                "SELECT pg_get_serial_sequence(%s, 'id')", (tbl,)
            )
            row = cur.fetchone()
            seq = row[0] if row else None
            if not seq:
                continue
            cur.execute(
                f"SELECT setval('{seq}', COALESCE((SELECT MAX(id) FROM {tbl}), 1))"
            )
        except Exception as exc:
            print(f"  [warn] could not bump sequence for {tbl}: {exc}")
    dst.commit()


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: migrate_sqlite_to_pg.py <path-to-sqlite.db>")
        return 2

    sqlite_path = Path(sys.argv[1])
    if not sqlite_path.exists():
        print(f"SQLite file not found: {sqlite_path}")
        return 1

    print(f"Source: {sqlite_path}")
    print(f"Target: {os.environ.get('DATABASE_URL', '(default postgres URL)')}")

    src = sqlite3.connect(str(sqlite_path))
    dst = db_engine.engine.raw_connection()
    try:
        dst.autocommit = False
    except Exception:
        pass

    try:
        total = 0
        for tbl in TABLES:
            total += migrate_table(src, dst, tbl)

        # Bump sequences so future INSERTs don't clash with imported ids.
        reset_serial_sequences(dst, [t for t in TABLES if t != "user_groups"])

        print(f"\nMigration complete. {total} rows inserted across {len(TABLES)} tables.")
        return 0
    finally:
        try:
            dst.close()
        except Exception:
            pass
        try:
            src.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
