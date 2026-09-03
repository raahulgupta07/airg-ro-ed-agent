#!/usr/bin/env python3
"""
PostgreSQL connection engine — replaces the previous direct sqlite3 driver.

Provides:
  * `engine`             — SQLAlchemy Engine (QueuePool, 10 + 10 conns).
  * `get_conn()`         — Returns a `Sqlite3CompatConnection` that mimics the
                            sqlite3 connection API used everywhere in
                            `database.py` (so we don't have to rewrite ~3000
                            lines of raw SQL).

The compatibility shim handles, on every `cursor.execute(sql, params)`:
  * `?` placeholder      → `%s` (psycopg3 paramstyle)
  * `INSERT OR REPLACE`  → `INSERT ... ON CONFLICT DO UPDATE` (best-effort
                            rewrite using the table's primary key)
  * `INSERT OR IGNORE`   → `INSERT ... ON CONFLICT DO NOTHING`
  * `PRAGMA …`           → no-op (Postgres equivalents handled at server level)
  * `datetime('now')`    → `now()`
  * `DATE('now')`        → `CURRENT_DATE`
  * `CURRENT_TIMESTAMP`  → unchanged (works in both)
  * `cursor.lastrowid`   → returned id from a transparent `RETURNING id`
  * `conn.row_factory = sqlite3.Row` → swap subsequent cursors to dict rows
                            (rows behave like dicts AND tuples — same as Row)

This minimises diffs to the existing data layer while giving us PG concurrency.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Iterable, Optional

from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://ro_ed:ro_ed@postgres:5432/ro_ed",
)

# QueuePool sized for ~10 concurrent users + a few background workers.
engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=10,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=3600,
    future=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# sqlite3-compat exception aliases (so `except sqlite3.OperationalError` keeps
# working without changing every call site).
# ─────────────────────────────────────────────────────────────────────────────

import psycopg  # noqa: E402

OperationalError = psycopg.errors.OperationalError
IntegrityError = psycopg.errors.IntegrityError
ProgrammingError = psycopg.errors.ProgrammingError
DatabaseError = psycopg.errors.DatabaseError


# ─────────────────────────────────────────────────────────────────────────────
# SQL syntax translation
# ─────────────────────────────────────────────────────────────────────────────

_QMARK_RE = re.compile(r"(?<!')\?(?!')")       # not-quoted-only is approximate
_PRAGMA_RE = re.compile(r"^\s*PRAGMA\s", re.IGNORECASE)
_INSERT_OR_REPLACE_RE = re.compile(
    r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)",
    re.IGNORECASE | re.DOTALL,
)
_INSERT_OR_IGNORE_RE = re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.IGNORECASE)
_DATETIME_NOW_RE = re.compile(r"datetime\('now'\)", re.IGNORECASE)
_DATE_NOW_RE = re.compile(r"DATE\('now'\)", re.IGNORECASE)
_AUTOINCREMENT_RE = re.compile(
    r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", re.IGNORECASE
)

# Tables where INSERT OR REPLACE is used → primary key column for upsert.
_PK_BY_TABLE = {
    "pdf_metadata": "job_id",
    "settings": "key",
}


def _qmark_to_pct(sql: str) -> str:
    """Replace `?` placeholders with `%s` (psycopg3 paramstyle).

    Naive but the codebase doesn't use ? inside string literals — verified.
    """
    return sql.replace("?", "%s")


def _rewrite_insert_or_replace(sql: str) -> str:
    """`INSERT OR REPLACE INTO t (a,b,c) VALUES (?,?,?)` →
    `INSERT INTO t (a,b,c) VALUES (?,?,?) ON CONFLICT (pk) DO UPDATE SET
        a = EXCLUDED.a, b = EXCLUDED.b, c = EXCLUDED.c`.
    """
    m = _INSERT_OR_REPLACE_RE.match(sql.strip())
    if not m:
        # Bare token replacement (rare, e.g. multi-row inserts) — best effort
        return re.sub(
            r"\bINSERT\s+OR\s+REPLACE\b",
            "INSERT",
            sql,
            flags=re.IGNORECASE,
        )

    table = m.group(1)
    cols_str = m.group(2)
    vals_str = m.group(3)
    pk = _PK_BY_TABLE.get(table.lower(), "id")
    cols = [c.strip() for c in cols_str.split(",")]
    set_clause = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in cols if c.lower() != pk.lower()
    )
    rebuilt = (
        f"INSERT INTO {table} ({cols_str}) VALUES ({vals_str}) "
        f"ON CONFLICT ({pk}) DO UPDATE SET {set_clause}"
    )
    # Keep any trailing SQL the regex didn't capture
    tail = sql[m.end():]
    return rebuilt + tail


def _translate(sql: str) -> str:
    """Apply all SQLite → Postgres syntax fixes."""
    if _PRAGMA_RE.match(sql):
        # Postgres has no PRAGMAs; turn into a no-op SELECT.
        return "SELECT 1"

    if "INSERT OR REPLACE" in sql.upper():
        sql = _rewrite_insert_or_replace(sql)
    if "INSERT OR IGNORE" in sql.upper():
        sql = _INSERT_OR_IGNORE_RE.sub("INSERT", sql)
        # Append ON CONFLICT DO NOTHING if not already present
        if "ON CONFLICT" not in sql.upper():
            # Strip trailing semicolons before append
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    sql = _DATETIME_NOW_RE.sub("now()", sql)
    sql = _DATE_NOW_RE.sub("CURRENT_DATE", sql)
    sql = _qmark_to_pct(sql)
    return sql


# ─────────────────────────────────────────────────────────────────────────────
# Compatibility wrapper
# ─────────────────────────────────────────────────────────────────────────────

class _DictRow(dict):
    """Behaves like sqlite3.Row: `row['col']` and `row[0]` and `dict(row)`."""

    def __init__(self, mapping, ordered_keys):
        super().__init__(mapping)
        self._keys_ordered = list(ordered_keys)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values_ordered()[key]
        return super().__getitem__(key)

    def _values_ordered(self):
        return [super(_DictRow, self).__getitem__(k) for k in self._keys_ordered]

    def keys(self):  # sqlite3.Row API
        return list(self._keys_ordered)

    def __iter__(self):
        # sqlite3.Row iterates values in column order, not keys
        return iter(self._values_ordered())


class _CursorWrapper:
    """Wraps a psycopg cursor, translates SQL, mimics sqlite3 cursor surface."""

    def __init__(self, raw_cur, dict_rows: bool):
        self._cur = raw_cur
        self._dict_rows = dict_rows
        self._last_returned_id: Optional[int] = None
        self._last_sql_was_returning_insert: bool = False

    # ── Statement execution ────────────────────────────────────────────────

    def execute(self, sql: str, params: Optional[Iterable] = None):
        translated = _translate(sql)

        # Auto-append RETURNING id on INSERTs so we can mimic lastrowid.
        # Heuristic: only for single-statement INSERT … VALUES … without an
        # existing RETURNING. We capture id into _last_returned_id.
        # Wrapped in a SAVEPOINT so a failed RETURNING (e.g. table has no
        # `id` column) doesn't poison the outer transaction.
        upper = translated.lstrip().upper()
        self._last_sql_was_returning_insert = False
        if (upper.startswith("INSERT INTO ")
                and " RETURNING " not in translated.upper()
                and " ON CONFLICT" not in translated.upper()):
            translated_with_ret = translated.rstrip().rstrip(";") + " RETURNING id"
            sp_name = "_sp_lastrowid"
            try:
                self._cur.execute(f"SAVEPOINT {sp_name}")
                self._cur.execute(translated_with_ret, params or ())
                row = self._cur.fetchone()
                self._cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                self._last_returned_id = row[0] if row else None
                self._last_sql_was_returning_insert = True
                return self
            except psycopg.errors.UndefinedColumn:
                # Table has no `id` PK (e.g. user_groups composite, settings.key,
                # pdf_metadata.job_id). Roll back to savepoint and re-run plain.
                try:
                    self._cur.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                except Exception:
                    pass
            except Exception:
                try:
                    self._cur.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                except Exception:
                    pass
                raise

        self._cur.execute(translated, params or ())
        self._last_returned_id = None
        return self

    def executemany(self, sql, seq_of_params):
        self._cur.executemany(_translate(sql), seq_of_params)
        return self

    # ── Fetching ───────────────────────────────────────────────────────────

    def _wrap(self, row):
        if row is None:
            return None
        # Convert datetime → str for SQLite-compat (Pydantic schemas expect str).
        import datetime as _dt
        def _conv(v):
            if isinstance(v, _dt.datetime):
                return v.strftime('%Y-%m-%d %H:%M:%S')
            if isinstance(v, _dt.date):
                return v.isoformat()
            return v
        if isinstance(row, dict):
            row = {k: _conv(v) for k, v in row.items()}
        elif isinstance(row, (tuple, list)):
            row = type(row)(_conv(v) for v in row)
        if self._dict_rows and self._cur.description is not None:
            cols = [d.name for d in self._cur.description]
            if isinstance(row, dict):
                return _DictRow(row, cols)
            return _DictRow(dict(zip(cols, row)), cols)
        return row

    def fetchone(self):
        # If we already drained the row off RETURNING id, signal that there is
        # no further user-visible row (sqlite3 INSERT yields no row either).
        if self._last_sql_was_returning_insert:
            self._last_sql_was_returning_insert = False
            return None
        try:
            row = self._cur.fetchone()
        except psycopg.ProgrammingError:
            return None
        return self._wrap(row)

    def fetchall(self):
        if self._last_sql_was_returning_insert:
            self._last_sql_was_returning_insert = False
            return []
        try:
            rows = self._cur.fetchall()
        except psycopg.ProgrammingError:
            return []
        return [self._wrap(r) for r in rows]

    def fetchmany(self, size=None):
        try:
            rows = self._cur.fetchmany(size) if size else self._cur.fetchmany()
        except psycopg.ProgrammingError:
            return []
        return [self._wrap(r) for r in rows]

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def lastrowid(self):
        return self._last_returned_id

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


class _Sqlite3CompatConnection:
    """Wraps a raw psycopg connection, mimicking sqlite3.Connection.

    * `execute(sql, params)` opens a transient cursor and runs it (used in many
      `conn.execute(...)`-as-shortcut sites).
    * `cursor()` returns a `_CursorWrapper`.
    * `row_factory = sqlite3.Row` (or anything truthy) toggles dict rows.
    * `commit()`, `rollback()`, `close()` proxy through.
    """

    def __init__(self, raw_conn):
        self._raw = raw_conn
        self._dict_rows = False
        # Postgres autocommit-off by default. Make sure we're in tx mode so
        # commit()/rollback() match sqlite3 semantics.
        try:
            self._raw.autocommit = False
        except Exception:
            pass

    # ── sqlite3 surface area ──────────────────────────────────────────────

    @property
    def row_factory(self):
        return self._dict_rows

    @row_factory.setter
    def row_factory(self, value):
        # sqlite3.Row → dict rows. None → tuple rows.
        self._dict_rows = bool(value)

    def cursor(self, *args, **kwargs):
        # If caller passes row_factory=dict_row directly we honour it.
        cur = self._raw.cursor()
        return _CursorWrapper(cur, self._dict_rows)

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params or ())
        return cur

    def executemany(self, sql, seq_of_params):
        cur = self.cursor()
        cur.executemany(sql, seq_of_params)
        return cur

    def commit(self):
        try:
            self._raw.commit()
        except Exception as exc:
            # A failed commit means the write is GONE. Swallowing it (the old
            # behaviour) reported success to the caller and silently lost data.
            logger.error("commit() failed — rolling back and re-raising: %s", exc)
            try:
                self._raw.rollback()
            except Exception:
                pass
            raise

    def rollback(self):
        try:
            self._raw.rollback()
        except Exception:
            pass

    def close(self):
        try:
            # Always rollback any abandoned tx before returning to pool.
            try:
                self._raw.rollback()
            except Exception:
                pass
            self._raw.close()
        except Exception:
            pass

    # Some callers use the connection as a context manager.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc:
            self.rollback()
        else:
            self.commit()
        self.close()


def get_conn() -> _Sqlite3CompatConnection:
    """Get a pooled raw psycopg connection wrapped in the sqlite3-compat shim."""
    raw = engine.raw_connection()
    return _Sqlite3CompatConnection(raw)


# Keep a thread lock for any one-off operations that need serialisation.
_init_lock = threading.Lock()

# Postgres session-level advisory lock guarding the Alembic migration run.
# uvicorn boots with `--workers 2`, and every worker process calls
# `database.init_database()` → `run_alembic_upgrade()`. Without this, both
# processes walk the migration chain against the same database at the same time
# and the loser dies ("Running upgrade -> 0001_initial" twice); uvicorn's parent
# then kills the sibling mid-migration. Arbitrary but FIXED 64-bit key — it must
# differ from the one `database.init_database()` takes on its own connection
# (823651749), or a single process would block waiting on its own other session.
_ALEMBIC_LOCK_KEY = 4115206649


# ─────────────────────────────────────────────────────────────────────────────
# Schema contract — what the live schema must look like after migrations
#
# There are TWO authors of DDL in this repo and they disagree:
#   * `alembic/versions/` — owns the schema in practice.
#   * `database.init_database()` — `CREATE TABLE IF NOT EXISTS` plus a long list
#     of `ALTER TABLE … ADD COLUMN IF NOT EXISTS`.
#
# The second one cannot fix a wrong TYPE. `ADD COLUMN IF NOT EXISTS` on a column
# that already exists is a no-op — it never ALTERs. So `database.py:552` has said
# `items.customs_value_mmk DOUBLE PRECISION` for a long time while the live column
# stayed `real`, because 0001 created it REAL. Editing the CREATE TABLE text in
# `database.py` changes nothing on any database that already exists. That edit
# looks correct, reviews clean, and is inert.
#
# This table is the third opinion, and it is checked against the live database at
# boot rather than against either source file — so it cannot be fooled by a fix
# that was never applied.
# ─────────────────────────────────────────────────────────────────────────────

# Postgres `real` is IEEE-754 single precision: ~7 significant decimal digits
# (6 guaranteed). A Myanmar customs value in kyats routinely needs 10:
# 19,363,898.77 stores and reads back as 19,363,898.0. Nothing errors — a float
# column got a valid float — and the wrong number reaches the customer's Excel.
_ACCEPTABLE_MONEY_TYPES = frozenset({"double precision", "numeric"})

#: (table, column) -> why this column carries more digits than `real` can hold.
MONEY_COLUMNS = {
    # declarations — widened by migration 0006. Pinned here so a later migration
    # (or a `downgrade`) cannot quietly narrow them back.
    ("declarations", "invoice_price"): "invoice-currency amount, 10+ sig digits",
    ("declarations", "invoice_price_fc"): "invoice-currency amount",
    ("declarations", "invoice_price_mmk"): "kyat amount, 10-11 sig digits",
    ("declarations", "exchange_rate"): "rate printed to 7 dp (67.2133333)",
    ("declarations", "freight_value"): "CIF build-up, invoice currency",
    ("declarations", "insurance_value"): "CIF build-up, invoice currency",
    ("declarations", "adjustment_value"): "CIF build-up, invoice currency",
    ("declarations", "total_customs_value"): "kyat amount, 10-11 sig digits",
    ("declarations", "import_export_customs_duty"): "kyat tax amount",
    ("declarations", "commercial_tax_ct"): "kyat tax amount",
    ("declarations", "advance_income_tax_at"): "kyat tax amount",
    ("declarations", "security_fee_sf"): "kyat tax amount",
    ("declarations", "maccs_service_fee_mf"): "kyat tax amount",
    ("declarations", "exemption_reduction"): "kyat amount",
    # items — `customs_value_mmk` is the column that actually shipped truncated
    # figures to a customer. The other four were `text`, which is a different
    # defect with the same cause: nobody ever said what the column holds. A
    # number kept as text does not round, but `'9' > '10'` is true and SUM() does
    # not typecheck, so the per-row gate (value ≈ qty × price × rate) was doing
    # string maths on three of its four inputs.
    ("items", "customs_value_mmk"): "kyat line value, 10-11 sig digits",
    ("items", "quantity"): "fractional packs (236.16) — must be a number",
    ("items", "invoice_unit_price"): "unit price multiplied BY a rate",
    ("items", "cif_unit_price"): "unit price multiplied BY a rate",
    ("items", "exchange_rate"): "rate, 61.95007144978846 — must be a number",
    # Percentages, but inputs to a monetary product (tax = value × rate), so a
    # float32 artefact in the rate lands in a kyat figure. They belong to the
    # contract for their TYPE, not for their magnitude — see PERCENTAGE_COLUMNS.
    ("items", "customs_duty_rate"): "duty %, multiplied into a kyat amount",
    ("items", "commercial_tax_percent"): "tax %, multiplied into a kyat amount",
    # importer_profiles — learned FX bands. `real` here rounds 67.2133333 to
    # 67.21333, which then reads as drift against a correctly-read rate.
    ("importer_profiles", "exchange_rate_min"): "rate, 9+ sig digits",
    ("importer_profiles", "exchange_rate_max"): "rate, 9+ sig digits",
    ("importer_profiles", "exchange_rate_avg"): "rate, 9+ sig digits",
    # jobs — per-call LLM spend goes below $0.0001; float4 quantises the cost
    # dashboard. Widened to numeric(20,8) by 0007 rather than the money default.
    ("jobs", "cost_usd"): "USD spend below $0.0001 per call",
}

#: (table, column) -> the exact `numeric(precision, scale)` the column must have.
#:
#: The width is load-bearing, not cosmetic. `numeric` with no typmod is arbitrary
#: precision and always safe, but a DECLARED width silently rounds anything wider:
#: `numeric(20,4)` on an exchange rate would keep 4 of the 10 decimals a real
#: ledger rate carries (61.95007144978846), which is the same defect as `real`
#: wearing a different type name. Checking only `data_type = 'numeric'` would pass
#: on exactly that column, so the widths are pinned here.
#:
#: Scales come from migration 0007, chosen by use rather than by name:
#:   (20,4)  money — kyat amounts run to 10-11 digits before the point
#:   (24,10) rate-like, and unit prices, which get multiplied BY a rate
#:   (24,6)  quantity — fractional packs (236.16) are normal
#:   (9,4)   percentages that feed a monetary product
#:   (20,8)  per-job LLM spend, which goes below $0.0001 per call
MONEY_COLUMN_WIDTHS = {
    ("items", "customs_value_mmk"): (20, 4),
    ("items", "quantity"): (24, 6),
    ("items", "invoice_unit_price"): (24, 10),
    ("items", "cif_unit_price"): (24, 10),
    ("items", "exchange_rate"): (24, 10),
    ("items", "customs_duty_rate"): (9, 4),
    ("items", "commercial_tax_percent"): (9, 4),
    ("importer_profiles", "exchange_rate_min"): (24, 10),
    ("importer_profiles", "exchange_rate_max"): (24, 10),
    ("importer_profiles", "exchange_rate_avg"): (24, 10),
    ("jobs", "cost_usd"): (20, 8),
}

#: The money columns that hold a PERCENTAGE, not an amount.
#:
#: They are in MONEY_COLUMNS because of what a wrong TYPE costs — a float32
#: artefact in a duty rate lands in a kyat figure — but their MAGNITUDE is a
#: fraction: the live corpus runs 0.03 to 0.15, and `numeric(9,4)` (above) caps
#: them at 99,999.9999 on purpose. A probe that writes a kyat amount into one of
#: them therefore overflows before it can measure anything, which is not a
#: finding about storage.
#:
#: Nor can a smaller probe rescue it: at four decimals and a magnitude under 1,
#: float32 reads back exactly (`real` returns 0.05 as '0.05'), so no value these
#: columns can legally hold tells correct storage apart from `real`. A value
#: probe here would pass on the defect, so it is skipped and their type and
#: width are left to `find_schema_drift`, which rejects `real` outright.
PERCENTAGE_COLUMNS = {
    ("items", "customs_duty_rate"),
    ("items", "commercial_tax_percent"),
}

#: (table, column) -> why `real` is deliberately fine here. Anything typed `real`
#: that is in neither this map nor MONEY_COLUMNS is reported as unclassified —
#: that is how the NEXT money column typed `real` gets noticed instead of
#: shipping rounded numbers for a year.
TOLERATED_REAL_COLUMNS = {
    ("jobs", "accuracy_percent"): "diagnostic percentage, never multiplied in",
    ("jobs", "processing_time_seconds"): "diagnostic timing, not money",
    ("processing_logs", "duration_seconds"): "diagnostic timing, not money",
    ("page_extractions", "confidence"): "0..1 model score, not money",
}

#: `*_json` columns deliberately left as `text`. Empty today. A column belongs
#: here only when a reader would break on jsonb (psycopg returns jsonb already
#: parsed, so any call site doing `json.loads(row["x_json"])` fails on a dict).
TOLERATED_TEXT_JSON_COLUMNS: set = set()

_JSON_COLUMN_SUFFIX = "_json"

# Last drift report, so a health endpoint can surface it without re-querying.
# `None` = the check has not run yet (or could not run).
_drift_findings: Optional[list] = None
_drift_error: Optional[str] = None


def _live_column_types(conn) -> dict:
    """`{(table, column): type}` for the schema the app actually writes to.

    `format_type` rather than `information_schema.data_type`, so the value
    carries the typmod: `numeric(20,4)`, not just `numeric`. The base spellings
    are identical for everything the contract checks (`real`, `text`, `jsonb`,
    `double precision`), so the extra detail costs nothing and a declared width
    that silently rounds becomes visible.

    Filtered to `current_schema()` — the same resolution an unqualified
    `ALTER TABLE declarations …` gets, so we compare like with like.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod) "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        )
        return {(r[0], r[1]): r[2] for r in cur.fetchall()}
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _base_type(sql_type: str) -> str:
    """`numeric(20,4)` -> `numeric`. Leaves typmod-free spellings untouched."""
    return sql_type.split("(", 1)[0].strip() if sql_type else sql_type


def _numeric_width(sql_type: str):
    """`(precision, scale)` from `numeric(20,4)`, or None when not declared.

    A bare `numeric` has no width: arbitrary precision, always safe, and nothing
    to check. Absent is different from wrong, and is not reported as drift.
    """
    if not sql_type or not sql_type.startswith("numeric("):
        return None
    try:
        inner = sql_type[sql_type.index("(") + 1:sql_type.rindex(")")]
        parts = [int(p.strip()) for p in inner.split(",")]
    except Exception:
        return None
    if len(parts) == 1:
        return (parts[0], 0)
    return (parts[0], parts[1]) if len(parts) == 2 else None


def find_schema_drift(types: dict) -> list:
    """Compare a `{(table, column): data_type}` map against the contract above.

    Pure — takes the schema as data so it is testable without a database.
    Returns a list of `{severity, table, column, expected, actual, detail}`,
    empty when the live schema matches. Never raises.
    """
    findings = []

    for (table, column), why in sorted(MONEY_COLUMNS.items()):
        raw = types.get((table, column))
        actual = _base_type(raw)
        if actual is None:
            # Only a finding if the table exists — a database that has never had
            # `importer_profiles` created is not drifted, it is just younger.
            if any(t == table for t, _ in types):
                findings.append({
                    "severity": "error", "table": table, "column": column,
                    "expected": "numeric or double precision", "actual": None,
                    "detail": f"money column is missing ({why})",
                })
            continue
        if actual not in _ACCEPTABLE_MONEY_TYPES:
            if actual == "real":
                harm = ("single precision keeps ~7 significant digits: "
                        "19,363,898.77 reads back as 19,363,898.0")
            elif actual in ("text", "character varying"):
                harm = ("a number kept as text cannot be summed or compared "
                        "numerically: '9' > '10' is true")
            else:
                harm = "not a numeric type that preserves the printed figure"
            findings.append({
                "severity": "error", "table": table, "column": column,
                "expected": "numeric or double precision", "actual": actual,
                "detail": f"{why}; stored as {actual} — {harm}",
            })
            continue

        # Right base type, possibly the wrong width. A DECLARED numeric width
        # rounds anything wider, so `numeric(20,4)` on a 10-decimal exchange rate
        # is the same defect as `real` under a different type name — and a check
        # that stopped at `data_type = 'numeric'` would pass it.
        want = MONEY_COLUMN_WIDTHS.get((table, column))
        have = _numeric_width(raw)
        if want and have and have != want:
            findings.append({
                "severity": "error", "table": table, "column": column,
                "expected": f"numeric{want}".replace(" ", ""), "actual": raw,
                "detail": (f"{why}; declared width keeps {have[1]} decimal "
                           f"place(s), the contract needs {want[1]}"),
            })

    for (table, column), raw in sorted(types.items()):
        actual = _base_type(raw)
        if not column.endswith(_JSON_COLUMN_SUFFIX):
            continue
        if (table, column) in TOLERATED_TEXT_JSON_COLUMNS:
            continue
        if actual == "text":
            findings.append({
                "severity": "error", "table": table, "column": column,
                "expected": "jsonb", "actual": actual,
                "detail": "JSON column still text — no validation, no indexing, "
                          "and a malformed write is only discovered on read",
            })

    for (table, column), raw in sorted(types.items()):
        actual = _base_type(raw)
        if actual != "real":
            continue
        if (table, column) in MONEY_COLUMNS:
            continue  # already reported above as an error
        if (table, column) in TOLERATED_REAL_COLUMNS:
            continue
        findings.append({
            "severity": "warning", "table": table, "column": column,
            "expected": "double precision or an entry in TOLERATED_REAL_COLUMNS",
            "actual": actual,
            "detail": "unclassified `real` column — if it holds money or a rate "
                      "it is silently rounding; if not, list it as tolerated",
        })

    return findings


def check_schema_drift(conn=None) -> list:
    """Query the live schema and record any divergence from the contract.

    Never raises: a failure to CHECK is not a failure of the schema, and this
    runs on the boot path. On error the status becomes "unknown" rather than
    "ok" — an unrunnable check must not read as a clean bill of health.
    """
    global _drift_findings, _drift_error

    own_conn = conn is None
    raw = None
    try:
        raw = engine.raw_connection() if own_conn else conn
        types = _live_column_types(raw)
        _drift_findings = find_schema_drift(types)
        _drift_error = None
        return _drift_findings
    except Exception as exc:
        _drift_findings = None
        _drift_error = f"{type(exc).__name__}: {exc}"
        logger.warning("schema drift check could not run: %s", _drift_error)
        return []
    finally:
        if own_conn and raw is not None:
            try:
                raw.rollback()
            except Exception:
                pass
            try:
                raw.close()
            except Exception:
                pass


def _log_schema_drift(findings: list) -> None:
    """Log drift at ERROR, one line per finding, with a banner.

    ── Why this LOGS rather than raises ───────────────────────────────────────
    Raising here would be both dangerous and useless.

    Useless, because of the caller. `database.init_database()` (database.py:88-92)
    already does:

        try:
            db_engine.run_alembic_upgrade()
        except Exception as exc:
            logger.debug("alembic upgrade skipped: %s", exc)

    — every exception out of this function is swallowed at DEBUG level and boot
    continues regardless. A raise would produce a line nobody reads, on a level
    nobody enables, and change nothing about whether the app starts.

    Dangerous, because the one caller that might NOT swallow it is a production
    deploy. Wedging every container on a type mismatch is a far worse outcome
    than serving rounded numbers for another hour, and the operator has no way to
    override it at 3am.

    So: log at ERROR (unmissable in `docker logs`), keep the result on the module
    so `/api/health` can report it, and give tests and CI an explicit
    `assert_no_schema_drift()` that DOES raise — which is where a hard failure
    belongs, before the deploy rather than during it.
    """
    if not findings:
        return
    errors = [f for f in findings if f["severity"] == "error"]
    logger.error(
        "%s\nSCHEMA DRIFT: %d finding(s), %d error(s). The live database does "
        "not match the contract in db_engine.MONEY_COLUMNS / *_json.\n"
        "Migrations own the schema — editing CREATE TABLE in database.py does "
        "NOT change an existing column, and ADD COLUMN IF NOT EXISTS never "
        "ALTERs a type. Fix this with a migration.\n%s",
        "=" * 78, len(findings), len(errors), "=" * 78,
    )
    for f in findings:
        logger.error(
            "SCHEMA DRIFT [%s] %s.%s: expected %s, found %s — %s",
            f["severity"], f["table"], f["column"],
            f["expected"], f["actual"], f["detail"],
        )


def schema_drift_status() -> dict:
    """Drift state for `/api/health`, safe to serialise.

    `status` is one of:
      * `"ok"`      — checked, contract holds.
      * `"drift"`   — checked, the live schema diverges. `errors` > 0 means a
                      money column is rounding or a JSON column is untyped.
      * `"unknown"` — the check has not run yet, or could not run. Deliberately
                      NOT "ok": an unrunnable check is not a passing one.

    Wiring is one line in `main.py` `health_check()`:
        "schema": db_engine.schema_drift_status(),
    """
    if _drift_findings is None:
        return {"status": "unknown", "error": _drift_error,
                "errors": 0, "warnings": 0, "findings": []}
    errors = [f for f in _drift_findings if f["severity"] == "error"]
    warnings = [f for f in _drift_findings if f["severity"] == "warning"]
    return {
        "status": "drift" if _drift_findings else "ok",
        "error": None,
        "errors": len(errors),
        "warnings": len(warnings),
        "findings": _drift_findings,
    }


class SchemaDriftError(RuntimeError):
    """Raised only by `assert_no_schema_drift()` — never on the boot path."""


def assert_no_schema_drift(conn=None, include_warnings: bool = False) -> None:
    """Raise `SchemaDriftError` if the live schema diverges. For tests and CI.

    This is the strict half of the contract, deliberately kept out of boot. Run
    it in CI against a migrated database, or as a post-deploy smoke check, so the
    failure lands where someone can act on it.
    """
    findings = check_schema_drift(conn)
    if _drift_error:
        raise SchemaDriftError(f"could not check schema: {_drift_error}")
    if not include_warnings:
        findings = [f for f in findings if f["severity"] == "error"]
    if findings:
        lines = "\n".join(
            f"  {f['severity']}: {f['table']}.{f['column']} — expected "
            f"{f['expected']}, found {f['actual']} ({f['detail']})"
            for f in findings
        )
        raise SchemaDriftError(f"{len(findings)} schema drift finding(s):\n{lines}")


def run_alembic_upgrade(target: str = "head") -> None:
    """Run Alembic migrations against `engine`. Used by `database.init_database()`."""
    from pathlib import Path
    from alembic.config import Config
    from alembic import command

    here = Path(__file__).parent
    cfg_path = here / "alembic.ini"
    if not cfg_path.exists():
        logger.warning("alembic.ini not found at %s — skipping migration", cfg_path)
        # Still audit the schema. An unmigrated database is exactly the case
        # where drift is most likely, so skipping the check here would blind us
        # precisely when it matters.
        _log_schema_drift(check_schema_drift())
        return

    cfg = Config(str(cfg_path))
    cfg.set_main_option("script_location", str(here / "alembic"))
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL)

    # Serialise across processes on ONE dedicated pooled connection: advisory
    # locks are session-scoped, so lock and unlock must happen on the same
    # session. Alembic opens its own connections from `sqlalchemy.url`, so it
    # never touches this one. The loser blocks in pg_advisory_lock() until the
    # winner unlocks, then runs `upgrade head` as a no-op — it never returns
    # before the schema is actually at head.
    try:
        _run_alembic_locked(cfg, target)
    finally:
        # Audit AFTER the migrations and OUTSIDE the advisory lock — the check is
        # a read-only query and must never hold the lock every other booting
        # worker is queued behind.
        #
        # In a `finally` on purpose: a migration that FAILED is the moment drift
        # matters most. `database.init_database()` (database.py:88-92) catches
        # that failure at DEBUG level and carries on into its own
        # `CREATE TABLE IF NOT EXISTS` block, so a broken migration is followed
        # by the app running on whatever schema `database.py` describes — with
        # no message above debug level anywhere. This line is what makes that
        # visible.
        #
        # `check_schema_drift` never raises, so the caller's contract is
        # unchanged: a clean migration still returns None quietly, and a failed
        # one still propagates the original exception, not one from the audit.
        _log_schema_drift(check_schema_drift())


def _run_alembic_locked(cfg, target: str) -> None:
    """The original migration body, unchanged, extracted so the drift audit in
    `run_alembic_upgrade` can sit in a `finally` around it."""
    from alembic import command

    with _init_lock:
        lock_conn = engine.raw_connection()
        try:
            cur = lock_conn.cursor()
            cur.execute("SELECT pg_advisory_lock(%s)", (_ALEMBIC_LOCK_KEY,))
            cur.close()
            # End the lock transaction — a session-level advisory lock survives
            # commit, and we don't want an idle-in-transaction session parked
            # for the whole migration.
            lock_conn.commit()
            try:
                command.upgrade(cfg, target)
            finally:
                # Always release, even on a failed migration — a held lock would
                # otherwise wedge every future boot.
                try:
                    cur = lock_conn.cursor()
                    cur.execute("SELECT pg_advisory_unlock(%s)", (_ALEMBIC_LOCK_KEY,))
                    cur.close()
                    lock_conn.commit()
                except Exception as exc:
                    logger.warning("failed to release alembic advisory lock: %s", exc)
        finally:
            # Closing returns the session to the pool; do it after the unlock so
            # no pooled connection is handed out still holding the lock.
            try:
                lock_conn.close()
            except Exception:
                pass
