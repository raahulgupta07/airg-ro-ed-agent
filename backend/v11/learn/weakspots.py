#!/usr/bin/env python3
"""
LEARNER — weak-spot detection (V11 advisory layer)
==================================================

Learns *which fields the extractors get wrong most often* so the pipeline can
spend extra verification effort ONLY on those fields. A "weak spot" is a field
with a high observed error rate; the handwriting engine can then vote more
times on weak fields and leave the reliable ones alone — saving tokens while
raising accuracy where it actually matters.

Design notes — this module is HUMAN-APPROVABLE and AUDITABLE
------------------------------------------------------------
* **Derived from logged signals, nothing invented.** Weak spots come straight
  from two audit tables a human can inspect:
    1. ``field_accuracy`` (preferred) — per ``(importer, field)`` counters of
       ``total_extractions`` and ``corrections_count``. The error rate is
       simply ``corrections_count / total_extractions``.
    2. ``field_edits`` (fallback) — the per-cell review audit trail. If
       ``field_accuracy`` is empty, we treat "a field humans edit a lot" as a
       proxy for weakness: count edits per ``field_name`` and normalize the
       counts into a 0..1 score (most-edited field = 1.0).
  Either way every score traces back to logged rows; the computation is
  deterministic and re-runnable.
* **Advisory only.** Nothing here changes an extracted value. It produces a
  *vote plan* the engine may use to allocate extra effort — never a hard edit.
* **Fails safe.** All DB access is wrapped in try/except. On an empty DB,
  missing tables, or any driver error this module degrades to ``{}`` / ``[]``
  rather than raising — it must never break the extraction pipeline.

DB layer
--------
Uses the same sqlite3-compatible shim as the rest of the backend
(``database._connect()`` → psycopg pooled connection). SQL uses ``?``
placeholders (auto-translated to ``%s``) and ``conn.row_factory = sqlite3.Row``
for dict-like rows. See ``backend/db_engine.py``.

CLI
---
    python -m v11.learn.weakspots [importer]   # print weak fields + vote plan
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Reuse the priors module's importer normalizer so keying matches everywhere.
# Imported lazily-safe: if it (or the DB shim) can't load, every public
# function still degrades gracefully to {} / [].
try:  # pragma: no cover - import guard
    from v11.learn.priors import _norm_importer  # type: ignore
except Exception:  # pragma: no cover
    try:
        from priors import _norm_importer  # type: ignore
    except Exception:  # pragma: no cover
        import re as _re

        _SPACE_RE = _re.compile(r"\s+")

        def _norm_importer(name: str) -> str:  # type: ignore
            if not name or not isinstance(name, str):
                return ""
            return _SPACE_RE.sub(" ", name.strip()).upper()

try:  # pragma: no cover - import guard
    import database  # type: ignore
    from database import sqlite3  # the _SqliteShim (sqlite3.Row sentinel)
except Exception:  # pragma: no cover
    database = None  # type: ignore
    sqlite3 = None  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────

#: A field needs at least this many extractions before its error rate from
#: ``field_accuracy`` is trusted (avoids 1/1 = 100% noise on tiny samples).
MIN_EXTRACTIONS_FOR_RATE = 3

#: Default error-rate cut-off above which a field counts as "weak".
DEFAULT_THRESHOLD = 0.2


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────

def _safe_close(conn) -> None:
    """Close a connection without ever raising."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def _rates_from_field_accuracy(cursor, norm: Optional[str]) -> Dict[str, float]:
    """Error rates from the ``field_accuracy`` counters.

    ``error_rate = corrections_count / total_extractions``. When an importer is
    given we scope to that importer; otherwise we aggregate counters across all
    importers per ``field_key`` before dividing (so the global rate reflects the
    whole corpus, not a per-row average). Rows with too few extractions are
    ignored. Returns ``{}`` if the table is empty / missing.
    """
    rates: Dict[str, float] = {}
    if norm:
        cursor.execute(
            """
            SELECT field_key,
                   SUM(total_extractions) AS total,
                   SUM(corrections_count) AS errors
            FROM field_accuracy
            WHERE importer_name_normalized = ?
            GROUP BY field_key
            """,
            (norm,),
        )
    else:
        cursor.execute(
            """
            SELECT field_key,
                   SUM(total_extractions) AS total,
                   SUM(corrections_count) AS errors
            FROM field_accuracy
            GROUP BY field_key
            """
        )
    for r in cursor.fetchall():
        row = dict(r)
        field = (row.get("field_key") or "").strip()
        if not field:
            continue
        total = row.get("total") or 0
        errors = row.get("errors") or 0
        try:
            total = int(total)
            errors = int(errors)
        except (TypeError, ValueError):
            continue
        if total < MIN_EXTRACTIONS_FOR_RATE:
            continue
        rate = errors / total if total > 0 else 0.0
        # Clamp into 0..1 (defensive against bad counters).
        rates[field] = max(0.0, min(1.0, rate))
    return rates


def _rates_from_field_edits(cursor, norm: Optional[str]) -> Dict[str, float]:
    """Proxy error rates from edit *frequency* in ``field_edits``.

    No accuracy counters exist yet, so we fall back to "more human edits ⇒
    weaker field". We count edits per ``field_name`` and normalize the counts
    so the most-edited field maps to ``1.0``. When an importer is given we join
    ``field_edits → jobs → declarations`` to scope by importer. Returns ``{}``
    if there are no edits.
    """
    counts: Dict[str, int] = {}
    if norm:
        # field_edits has no importer column; reach it through the job's
        # declaration importer_name (normalized the same way as priors).
        cursor.execute(
            """
            SELECT fe.field_name AS field_name
            FROM field_edits fe
            JOIN declarations d ON d.job_id = fe.job_id
            WHERE UPPER(TRIM(COALESCE(d.importer_name, ''))) = ?
            """,
            (norm,),
        )
    else:
        cursor.execute("SELECT field_name FROM field_edits")
    for r in cursor.fetchall():
        field = (dict(r).get("field_name") or "").strip()
        if not field:
            continue
        counts[field] = counts.get(field, 0) + 1
    if not counts:
        return {}
    peak = max(counts.values())
    if peak <= 0:
        return {}
    return {field: c / peak for field, c in counts.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def field_error_rates(importer_name: str = None) -> dict:
    """Return ``{field_key: error_rate}`` with each rate in ``0..1``.

    Source preference:
      1. ``field_accuracy`` table — ``corrections_count / total_extractions``.
      2. If that is empty, fall back to ``field_edits`` edit-frequency as a
         proxy (most-edited field ⇒ ``1.0``), normalized to ``0..1``.

    Scoped to ``importer_name`` (normalized) when given, otherwise global.
    Returns ``{}`` on an empty DB, missing tables, or any error — never raises.
    """
    if database is None:
        return {}

    norm = _norm_importer(importer_name) if importer_name else None

    conn = None
    try:
        conn = database._connect()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Preferred signal: logged accuracy counters.
        try:
            rates = _rates_from_field_accuracy(cursor, norm)
        except Exception as exc:  # missing table / driver error
            logger.debug("field_error_rates: field_accuracy query failed: %s", exc)
            rates = {}

        if rates:
            _safe_close(conn)
            return rates

        # Fallback signal: human edit frequency.
        try:
            rates = _rates_from_field_edits(cursor, norm)
        except Exception as exc:
            logger.debug("field_error_rates: field_edits query failed: %s", exc)
            rates = {}

        _safe_close(conn)
        return rates
    except Exception as exc:
        logger.debug("field_error_rates failed: %s", exc)
        _safe_close(conn)
        return {}


def weak_fields(importer_name: str = None, threshold: float = DEFAULT_THRESHOLD) -> list:
    """Fields whose error rate ``>= threshold``, sorted worst-first.

    These are the fields that warrant extra votes / a dedicated verify pass.
    Returns ``[]`` if there are no weak fields or on any error.
    """
    try:
        thr = float(threshold)
    except (TypeError, ValueError):
        thr = DEFAULT_THRESHOLD

    rates = field_error_rates(importer_name)
    if not rates:
        return []

    weak = [(field, rate) for field, rate in rates.items() if rate >= thr]
    # Worst-first; tie-break on field name for deterministic, auditable order.
    weak.sort(key=lambda fr: (-fr[1], fr[0]))
    return [field for field, _ in weak]


def vote_plan(
    importer_name: str = None,
    base_votes: int = 1,
    weak_votes: int = 3,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict:
    """Return ``{field_key: vote_count}`` for the WEAK fields only.

    Weak fields (error rate ``>= threshold``) map to ``weak_votes``; every other
    field is implicitly ``base_votes`` and is *not* included in the returned
    dict. The handwriting engine uses this to vote more on weak fields only,
    keeping cost down on reliable ones.

    Returns ``{}`` when there are no weak fields or on any error. If
    ``weak_votes <= base_votes`` there is nothing to gain, so an empty plan is
    returned.
    """
    try:
        base = int(base_votes)
        weak = int(weak_votes)
    except (TypeError, ValueError):
        return {}

    if weak <= base:
        return {}

    fields = weak_fields(importer_name, threshold)
    if not fields:
        return {}

    return {field: weak for field in fields}


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _cli(argv: List[str]) -> int:
    """Tiny CLI. Import-clean even without a DB; DB work degrades safely."""
    importer = argv[1] if len(argv) > 1 else None
    scope = importer if importer else "(global)"

    rates = field_error_rates(importer)
    weak = weak_fields(importer)
    plan = vote_plan(importer)

    print(f"Weak-spot report — scope: {scope}")
    print("\nField error rates (0..1):")
    if rates:
        for field, rate in sorted(rates.items(), key=lambda fr: (-fr[1], fr[0])):
            print(f"  {field:40s} {rate:.3f}")
    else:
        print("  (none — no field_accuracy / field_edits signal)")

    print(f"\nWeak fields (>= {DEFAULT_THRESHOLD}), worst-first:")
    print("  " + (", ".join(weak) if weak else "(none)"))

    print("\nVote plan (weak fields → extra votes):")
    print(json.dumps(plan, indent=2) if plan else "  {}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_cli(sys.argv))
