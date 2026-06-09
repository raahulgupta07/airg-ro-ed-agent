#!/usr/bin/env python3
"""
LEARNER — importer priors (V11 advisory layer)
==============================================

Learns per-importer *norms* from past extractions and exposes them for
cross-checks and (optional) auto-fill. Think of it as a memory of "what a
declaration from IMPORTER X usually looks like": which currency, what exchange
rate band, the usual consignor, the common HS codes, and a baseline for the
MACCS service fee + typical duty/CT ratios.

Design notes — this module is HUMAN-APPROVABLE and AUDITABLE
------------------------------------------------------------
* **Advisory only.** Priors NEVER overwrite extracted values automatically.
  `check_against_priors()` returns *warnings* a human reviewer can act on. The
  gate / router uses these as soft signals, not hard rejects.
* **Traceable to source rows.** Every prior is aggregated only from
  `review_status = 'approved'` jobs when that column is available — i.e. rows a
  human already trusted. The aggregation is deterministic and re-runnable, so
  any prior can be traced back to the approved declarations that produced it.
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
    python -m v11.learn.priors build [importer]   # (re)build priors, upsert
    python -m v11.learn.priors check              # demo check vs a fake decl
"""

from __future__ import annotations

import json
import logging
import re
from statistics import median
from typing import Dict, List, Optional

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

#: Minimum approved documents an importer needs before we build priors for it.
MIN_DOCS_FOR_PRIORS = 2

#: An exchange rate is treated as an outlier (and ignored) if it is more than
#: this multiple of the per-importer median rate.
OUTLIER_RATE_FACTOR = 3.0

#: How many top HS codes / consignor values to remember.
TOP_HS_CODES = 8

#: Tolerance band applied around the learned [min, max] exchange-rate range when
#: flagging a freshly-extracted rate. min*LOW .. max*HIGH.
RATE_BAND_LOW = 0.9
RATE_BAND_HIGH = 1.1


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_SPACE_RE = re.compile(r"\s+")


def _norm_importer(name: str) -> str:
    """Normalize an importer name for stable keying / matching.

    Uppercase, strip, and collapse internal whitespace runs to a single space.
    Returns ``""`` for falsy / non-string input.

    >>> _norm_importer("  Acme   Trading   Co ")
    'ACME TRADING CO'
    """
    if not name or not isinstance(name, str):
        return ""
    return _SPACE_RE.sub(" ", name.strip()).upper()


def _to_float(value) -> Optional[float]:
    """Best-effort float coercion. Returns None for null/blank/garbage.

    Tolerates strings with commas, currency symbols, or stray spaces (extracted
    values are messy). Non-positive values map to None (rates/values of 0 are
    treated as "missing", per the spec which ignores nulls/zeros).
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if f > 0 else None
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        s = re.sub(r"[^0-9.\-]", "", s)
        if not s or s in {"-", ".", "-."}:
            return None
        try:
            f = float(s)
        except (ValueError, TypeError):
            return None
        return f if f > 0 else None
    return None


def _most_frequent(values: List[str]) -> Optional[str]:
    """Return the most frequent non-empty string in ``values`` (or None)."""
    counts: Dict[str, int] = {}
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        counts[s] = counts.get(s, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _top_n(values: List[str], n: int) -> List[str]:
    """Return the ``n`` most frequent non-empty strings, most-common first."""
    counts: Dict[str, int] = {}
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        counts[s] = counts.get(s, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _ in ranked[:n]]


def _filter_rates(rates: List[float]) -> List[float]:
    """Drop nulls/zeros (already handled by _to_float) and >3x-median outliers."""
    clean = [r for r in rates if r and r > 0]
    if len(clean) < 2:
        return clean
    med = median(clean)
    if med <= 0:
        return clean
    return [r for r in clean if r <= med * OUTLIER_RATE_FACTOR]


def _has_review_status(cursor) -> bool:
    """True if the jobs table exposes review_status (so we can filter approved)."""
    try:
        cursor.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'jobs' AND column_name = 'review_status' LIMIT 1"
        )
        return cursor.fetchone() is not None
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────

def build_priors(importer_name: str = None) -> dict:
    """Aggregate APPROVED declarations into per-importer priors and upsert them.

    For each importer (optionally just ``importer_name``) with at least
    :data:`MIN_DOCS_FOR_PRIORS` approved documents, compute:

      * ``exchange_rate`` min / max / avg (nulls, zeros, and >3x-median
        outliers ignored),
      * ``common_consignor`` — most frequent ``consignor_name``,
      * ``common_hs`` — top HS codes seen on that importer's line items,
      * ``currency`` — most frequent declared currency,
      * ``fee_baseline`` — median MACCS service fee, plus typical duty / CT
        ratios (median of fee ÷ total_customs_value across approved docs).

    Results are upserted into ``importer_profiles`` (keyed on
    ``importer_name_normalized``). Returns a summary dict
    ``{importer: {...priors...}}``. Degrades to ``{}`` on any DB error.

    Priors are advisory and fully re-derivable from the approved source rows.
    """
    if database is None:
        return {}

    summary: Dict[str, dict] = {}
    conn = None
    try:
        conn = database._connect()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        approved_only = _has_review_status(cursor)
        join_approved = (
            "JOIN jobs j ON j.job_id = d.job_id AND j.review_status = 'approved'"
            if approved_only
            else "JOIN jobs j ON j.job_id = d.job_id"
        )

        # Pull all declaration rows we trust, with their importer.
        where = ""
        params: List = []
        if importer_name:
            where = " WHERE UPPER(TRIM(d.importer_name)) = ?"
            params.append(_norm_importer(importer_name))

        cursor.execute(
            f"""
            SELECT d.job_id            AS job_id,
                   d.importer_name     AS importer_name,
                   d.consignor_name    AS consignor_name,
                   d.currency          AS currency,
                   d.exchange_rate     AS exchange_rate,
                   d.total_customs_value          AS total_customs_value,
                   d.import_export_customs_duty   AS customs_duty,
                   d.commercial_tax_ct            AS commercial_tax_ct,
                   d.maccs_service_fee_mf         AS maccs_service_fee_mf
            FROM declarations d
            {join_approved}
            {where}
            """,
            params,
        )
        decl_rows = [dict(r) for r in cursor.fetchall()]
    except Exception as exc:  # missing tables / empty DB / driver error
        logger.debug("build_priors: declaration query failed: %s", exc)
        _safe_close(conn)
        return {}

    if not decl_rows:
        _safe_close(conn)
        return {}

    # Group declarations by normalized importer; track job_ids for the HS join.
    grouped: Dict[str, dict] = {}
    for row in decl_rows:
        norm = _norm_importer(row.get("importer_name") or "")
        if not norm:
            continue
        g = grouped.setdefault(
            norm,
            {
                "display_name": (row.get("importer_name") or "").strip(),
                "job_ids": [],
                "consignors": [],
                "currencies": [],
                "rates": [],
                "fees": [],
                "duty_ratios": [],
                "ct_ratios": [],
            },
        )
        g["job_ids"].append(row.get("job_id"))
        g["consignors"].append(row.get("consignor_name"))
        g["currencies"].append(row.get("currency"))

        rate = _to_float(row.get("exchange_rate"))
        if rate is not None:
            g["rates"].append(rate)

        fee = _to_float(row.get("maccs_service_fee_mf"))
        if fee is not None:
            g["fees"].append(fee)

        total = _to_float(row.get("total_customs_value"))
        if total:
            duty = _to_float(row.get("customs_duty"))
            ct = _to_float(row.get("commercial_tax_ct"))
            if duty is not None:
                g["duty_ratios"].append(duty / total)
            if ct is not None:
                g["ct_ratios"].append(ct / total)

    # Fetch HS codes per importer (items → jobs → this importer's job_ids).
    hs_by_importer = _fetch_hs_codes(conn, grouped)

    # Now compute priors per importer and upsert.
    for norm, g in grouped.items():
        if len(g["job_ids"]) < MIN_DOCS_FOR_PRIORS:
            continue

        rates = _filter_rates(g["rates"])
        rate_min = min(rates) if rates else None
        rate_max = max(rates) if rates else None
        rate_avg = (sum(rates) / len(rates)) if rates else None

        fee_baseline = {
            "maccs_service_fee_median": (median(g["fees"]) if g["fees"] else None),
            "duty_ratio_median": (median(g["duty_ratios"]) if g["duty_ratios"] else None),
            "ct_ratio_median": (median(g["ct_ratios"]) if g["ct_ratios"] else None),
            "sample_size": len(g["job_ids"]),
        }

        priors = {
            "importer_name": g["display_name"] or norm,
            "importer_name_normalized": norm,
            "currency": _most_frequent(g["currencies"]),
            "exchange_rate_min": rate_min,
            "exchange_rate_max": rate_max,
            "exchange_rate_avg": rate_avg,
            "common_consignor": _most_frequent(g["consignors"]),
            "common_hs": hs_by_importer.get(norm, []),
            "fee_baseline": fee_baseline,
            "total_jobs": len(g["job_ids"]),
        }

        if _upsert_profile(conn, priors):
            summary[norm] = priors

    _safe_close(conn)
    return summary


def _fetch_hs_codes(conn, grouped: Dict[str, dict]) -> Dict[str, List[str]]:
    """Top HS codes per normalized importer (items joined via their job's importer)."""
    result: Dict[str, List[str]] = {}
    if conn is None or not grouped:
        return result
    try:
        cursor = conn.cursor()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        for norm, g in grouped.items():
            job_ids = [jid for jid in g["job_ids"] if jid]
            if not job_ids:
                continue
            placeholders = ",".join(["?"] * len(job_ids))
            cursor.execute(
                f"SELECT hs_code FROM items WHERE job_id IN ({placeholders}) "
                f"AND COALESCE(is_deleted, 0) = 0",
                job_ids,
            )
            codes = [
                (dict(r).get("hs_code") or "").strip()
                for r in cursor.fetchall()
            ]
            result[norm] = _top_n(codes, TOP_HS_CODES)
    except Exception as exc:
        logger.debug("_fetch_hs_codes failed: %s", exc)
    return result


def _upsert_profile(conn, priors: dict) -> bool:
    """Insert or update the importer_profiles row keyed on normalized name.

    The shim's INSERT OR REPLACE only knows the `id` PK, so we do an explicit
    UPDATE-first / INSERT-if-missing against the unique normalized name. This is
    deterministic and audit-friendly (one row per importer).
    """
    if conn is None:
        return False
    norm = priors["importer_name_normalized"]
    common_items_json = json.dumps(priors.get("common_hs") or [])
    fee_json = json.dumps(priors.get("fee_baseline") or {})
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE importer_profiles
            SET importer_name = ?,
                currency = ?,
                exchange_rate_min = ?,
                exchange_rate_max = ?,
                exchange_rate_avg = ?,
                common_consignor = ?,
                common_items = ?,
                total_jobs = ?,
                fee_baseline_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE importer_name_normalized = ?
            """,
            (
                priors["importer_name"],
                priors.get("currency"),
                priors.get("exchange_rate_min"),
                priors.get("exchange_rate_max"),
                priors.get("exchange_rate_avg"),
                priors.get("common_consignor"),
                common_items_json,
                priors.get("total_jobs", 0),
                fee_json,
                norm,
            ),
        )
        if cursor.rowcount == 0:
            cursor.execute(
                """
                INSERT INTO importer_profiles
                    (importer_name, importer_name_normalized, currency,
                     exchange_rate_min, exchange_rate_max, exchange_rate_avg,
                     common_consignor, common_items, total_jobs, fee_baseline_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    priors["importer_name"],
                    norm,
                    priors.get("currency"),
                    priors.get("exchange_rate_min"),
                    priors.get("exchange_rate_max"),
                    priors.get("exchange_rate_avg"),
                    priors.get("common_consignor"),
                    common_items_json,
                    priors.get("total_jobs", 0),
                    fee_json,
                ),
            )
        conn.commit()
        return True
    except Exception as exc:
        logger.debug("_upsert_profile failed for %s: %s", norm, exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────

def get_priors(importer_name: str) -> dict:
    """Read the learned priors for ``importer_name`` (normalized lookup).

    Returns a dict with keys: importer_name, currency, exchange_rate_min/max/avg,
    common_consignor, common_hs (parsed list), fee_baseline (parsed dict),
    total_jobs. Returns ``{}`` if there is no profile or on any DB error.

    Used by the gate / router for cross-checking. Advisory only.
    """
    if database is None:
        return {}
    norm = _norm_importer(importer_name)
    if not norm:
        return {}

    conn = None
    try:
        conn = database._connect()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM importer_profiles "
            "WHERE importer_name_normalized = ? LIMIT 1",
            (norm,),
        )
        row = cursor.fetchone()
    except Exception as exc:
        logger.debug("get_priors failed for %s: %s", norm, exc)
        _safe_close(conn)
        return {}

    if not row:
        _safe_close(conn)
        return {}

    data = dict(row)
    _safe_close(conn)

    # Parse JSON columns back to native structures (defensive).
    common_hs: List[str] = []
    try:
        common_hs = json.loads(data.get("common_items") or "[]")
        if not isinstance(common_hs, list):
            common_hs = []
    except (json.JSONDecodeError, TypeError):
        common_hs = []

    fee_baseline: dict = {}
    try:
        fee_baseline = json.loads(data.get("fee_baseline_json") or "{}")
        if not isinstance(fee_baseline, dict):
            fee_baseline = {}
    except (json.JSONDecodeError, TypeError):
        fee_baseline = {}

    return {
        "importer_name": data.get("importer_name"),
        "importer_name_normalized": data.get("importer_name_normalized"),
        "currency": data.get("currency"),
        "exchange_rate_min": data.get("exchange_rate_min"),
        "exchange_rate_max": data.get("exchange_rate_max"),
        "exchange_rate_avg": data.get("exchange_rate_avg"),
        "common_consignor": data.get("common_consignor"),
        "common_hs": common_hs,
        "fee_baseline": fee_baseline,
        "total_jobs": data.get("total_jobs"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cross-check
# ─────────────────────────────────────────────────────────────────────────────

def check_against_priors(declaration: dict) -> list:
    """Cross-check a freshly-extracted declaration against learned priors.

    Returns a list of human-readable warning strings (advisory — never a hard
    block). Empty list if no priors exist for the importer, if the declaration
    has no importer name, or on any error.

    Current checks:
      * exchange_rate outside the learned band [min*0.9, max*1.1],
      * currency differs from the usual currency,
      * consignor differs from the usual consignor.
    """
    if not declaration or not isinstance(declaration, dict):
        return []

    importer = (
        declaration.get("importer_name")
        or declaration.get("Importer (Name)")
        or ""
    )
    priors = get_priors(importer)
    if not priors:
        return []

    warnings: List[str] = []
    label = priors.get("importer_name") or _norm_importer(importer)

    # ── Exchange rate band ──────────────────────────────────────────────────
    rate = _to_float(
        declaration.get("exchange_rate") or declaration.get("Exchange Rate")
    )
    rmin = priors.get("exchange_rate_min")
    rmax = priors.get("exchange_rate_max")
    if rate is not None and rmin and rmax:
        low = rmin * RATE_BAND_LOW
        high = rmax * RATE_BAND_HIGH
        if rate < low or rate > high:
            warnings.append(
                f"exchange_rate {_fmt(rate)} outside learned range "
                f"{_fmt(rmin)}-{_fmt(rmax)} for {label}"
            )

    # ── Currency ────────────────────────────────────────────────────────────
    currency = (declaration.get("currency") or declaration.get("Currency") or "").strip()
    usual_currency = (priors.get("currency") or "").strip()
    if currency and usual_currency and currency.upper() != usual_currency.upper():
        warnings.append(
            f"currency '{currency}' differs from usual '{usual_currency}' for {label}"
        )

    # ── Consignor ───────────────────────────────────────────────────────────
    consignor = (
        declaration.get("consignor_name")
        or declaration.get("Consignor (Name)")
        or ""
    ).strip()
    usual_consignor = (priors.get("common_consignor") or "").strip()
    if consignor and usual_consignor and consignor.upper() != usual_consignor.upper():
        warnings.append(
            f"consignor '{consignor}' differs from usual '{usual_consignor}' for {label}"
        )

    return warnings


def _fmt(value) -> str:
    """Format a number compactly for warning messages (no trailing .0 noise)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f == int(f):
        return str(int(f))
    return f"{f:.4g}"


def _safe_close(conn) -> None:
    """Close a connection without ever raising."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _cli(argv: List[str]) -> int:
    """Tiny CLI. Import-clean even without a DB; actual DB work degrades safely."""
    cmd = argv[1] if len(argv) > 1 else "help"

    if cmd == "build":
        importer = argv[2] if len(argv) > 2 else None
        result = build_priors(importer)
        print(json.dumps(result, indent=2, default=str))
        print(f"\nBuilt priors for {len(result)} importer(s).")
        return 0

    if cmd == "check":
        # Hardcoded fake declaration — demonstrates a wrong exchange rate.
        fake = {
            "importer_name": "ACME TRADING CO",
            "consignor_name": "SOME OTHER SUPPLIER LTD",
            "currency": "USD",
            "exchange_rate": 2100,  # deliberately wrong (e.g. should be ~50-65)
            "total_customs_value": 1000000,
        }
        warnings = check_against_priors(fake)
        print("Fake declaration:")
        print(json.dumps(fake, indent=2))
        print("\nWarnings:")
        if warnings:
            for w in warnings:
                print(f"  - {w}")
        else:
            print("  (none — no priors found for this importer)")
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_cli(sys.argv))
