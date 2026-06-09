#!/usr/bin/env python3
"""
LEARNER — few-shot correction memory (V11 advisory layer)
=========================================================

Turns *past human corrections* into compact example hints that can be appended
to an extractor prompt, so the system stops repeating the same mistake for a
given importer ("last time the exchange_rate read 2100, but the human fixed it
to 57.4 — don't do that again").

Where the examples come from
----------------------------
Every cell a reviewer edits in the Review UI writes a row into the
``field_edits`` audit table (job_id, entity_type, entity_index, field_name,
original_value, corrected_value, edited_by, edited_at, ...). This module reads
*only* that human-authored audit trail. Nothing here is model-generated; each
hint is traceable to a logged human edit, which makes the injected prompt block
**auditable**.

Design notes — HUMAN-SOURCED and ADVISORY
-----------------------------------------
* **Advisory only.** The rendered block is a *hint* appended to the prompt. It
  never overwrites an extracted value and never hard-blocks anything.
* **Per-importer.** Corrections are scoped to the importer by joining
  ``field_edits`` → ``declarations.importer_name`` on ``job_id`` and matching
  the normalized importer name (shared with ``priors._norm_importer``).
* **Most-recent-wins, de-duplicated by field.** If the same field was corrected
  several times we keep only the latest correction for that field, so the hint
  reflects the importer's current ground truth.
* **Fails safe.** Every DB access is wrapped in try/except. On an empty DB,
  missing tables, or any driver error this module degrades to ``[]`` / ``""``
  rather than raising — it must never break the extraction pipeline.

DB layer
--------
Uses the same sqlite3-compatible shim as the rest of the backend
(``database._connect()`` → psycopg pooled connection). SQL uses ``?``
placeholders (auto-translated to ``%s``) and ``conn.row_factory = sqlite3.Row``
for dict-like rows. See ``backend/db_engine.py``.

CLI
---
    python -m v11.learn.fewshot <importer>   # print the prompt block for X
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from v11.learn.priors import _norm_importer

logger = logging.getLogger(__name__)

# DB layer — imported lazily-safe. If the import itself fails (e.g. running in a
# context with no DB libs), every public function still degrades gracefully.
try:  # pragma: no cover - import guard
    import database  # type: ignore
    from database import sqlite3  # the _SqliteShim (sqlite3.Row sentinel)
except Exception:  # pragma: no cover
    database = None  # type: ignore
    sqlite3 = None  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────

#: Default number of distinct corrected fields to surface as hints.
DEFAULT_LIMIT = 5

#: Hard cap on how many raw edit rows we pull before de-duplicating by field.
#: Keeps the query bounded even for a heavily-edited importer.
MAX_ROWS_SCANNED = 200


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_close(conn) -> None:
    """Close a connection without ever raising."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def _clean(value) -> str:
    """Render a stored value as a short, single-line string for a hint."""
    if value is None:
        return ""
    s = str(value).strip()
    # Collapse newlines so a multi-line cell never breaks the prompt block.
    s = " ".join(s.split())
    if len(s) > 80:
        s = s[:77] + "..."
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────

def recent_corrections(importer_name: str, limit: int = DEFAULT_LIMIT) -> list:
    """Most recent human corrections for ``importer_name``'s jobs.

    Joins ``field_edits`` → ``declarations`` on ``job_id`` and filters by the
    normalized importer name, then keeps the **latest** correction per field
    (de-duplicated by ``field_name``).

    Returns a list of dicts ``{field, old, new, when}`` ordered most-recent
    first, at most ``limit`` entries. Returns ``[]`` if the importer is unknown,
    there are no corrections, or on any error (never raises).
    """
    if database is None:
        return []

    norm = _norm_importer(importer_name)
    if not norm:
        return []

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    if limit <= 0:
        return []

    conn = None
    try:
        conn = database._connect()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # Pull this importer's edits, newest first. We over-fetch (MAX_ROWS_SCANNED)
        # then de-dup by field in Python so "latest per field" is exact regardless
        # of how the driver orders ties.
        cursor.execute(
            """
            SELECT fe.field_name        AS field_name,
                   fe.original_value    AS original_value,
                   fe.corrected_value   AS corrected_value,
                   fe.edited_at         AS edited_at
            FROM field_edits fe
            JOIN declarations d ON d.job_id = fe.job_id
            WHERE UPPER(TRIM(d.importer_name)) = ?
              AND fe.field_name IS NOT NULL
              AND TRIM(fe.field_name) <> ''
            ORDER BY fe.edited_at DESC, fe.id DESC
            LIMIT ?
            """,
            (norm, MAX_ROWS_SCANNED),
        )
        rows = [dict(r) for r in cursor.fetchall()]
    except Exception as exc:  # missing tables / empty DB / driver error
        logger.debug("recent_corrections query failed for %s: %s", norm, exc)
        _safe_close(conn)
        return []

    _safe_close(conn)

    # De-dup by field — rows are newest-first, so the first time we see a field
    # is its most-recent correction. Skip no-op edits (old == new).
    seen: Dict[str, bool] = {}
    out: List[dict] = []
    for row in rows:
        field = _clean(row.get("field_name"))
        if not field or field in seen:
            continue
        old = _clean(row.get("original_value"))
        new = _clean(row.get("corrected_value"))
        if new == "" or new == old:
            continue
        seen[field] = True
        out.append(
            {
                "field": field,
                "old": old,
                "new": new,
                "when": _clean(row.get("edited_at")),
            }
        )
        if len(out) >= limit:
            break
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────────

def prompt_block(importer_name: str, limit: int = DEFAULT_LIMIT) -> str:
    """Render this importer's recent corrections as a short prompt hint block.

    Produces something like::

        Known corrections for importer ACME TRADING CO (apply if relevant):
          - exchange_rate should be 57.4 (a prior extraction wrongly read 2100)
          - origin_country should be IT
          - consignor_name should be SOME SUPPLIER LTD

    Returns ``""`` when there are no corrections (or on any error), so callers
    can append it unconditionally. Advisory only — these are logged human edits,
    not rules.
    """
    corrections = recent_corrections(importer_name, limit=limit)
    if not corrections:
        return ""

    label = _norm_importer(importer_name) or (importer_name or "").strip()
    lines = [f"Known corrections for importer {label} (apply if relevant):"]
    for c in corrections:
        field = c.get("field") or ""
        new = c.get("new") or ""
        old = c.get("old") or ""
        if old:
            lines.append(
                f"  - {field} should be {new} "
                f"(a prior extraction wrongly read {old})"
            )
        else:
            lines.append(f"  - {field} should be {new}")
    # Trailing newline so the block sits cleanly when appended to a prompt.
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _cli(argv: List[str]) -> int:
    """Tiny CLI. Import-clean even without a DB; actual DB work degrades safely."""
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    importer = argv[1]
    block = prompt_block(importer)
    if block:
        print(block, end="")
    else:
        print(f"(no logged corrections found for importer '{importer}')")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_cli(sys.argv))
