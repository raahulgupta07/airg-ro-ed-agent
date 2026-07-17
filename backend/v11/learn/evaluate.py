#!/usr/bin/env python3
"""LEARNER — ALMA-inspired eval / archive / promote loop (Phase-6).

ALMA ("Assess, Learn, Measure, Adopt") is a discipline, not a model: never adopt
a prompt / model / config change because it *looks* better — adopt it only after
it demonstrably scores higher against approved truth. This module is that gate.

The golden corpus (``v11.learn.golden``) reconstructs ground truth from every
human-APPROVED job — the reviewer already verified those declarations + items.
Here we:

  1. **Assess**   — re-extract each golden PDF with a candidate engine/model and
                    compare field-by-field against the approved declaration.
  2. **Measure**  — reduce those comparisons to honest numbers (field accuracy,
                    item recall, per-field accuracy, cost, skips).
  3. **Archive**  — persist each measured score in the settings kv store so a
                    bake-off has a durable baseline to beat.
  4. **Adopt**    — ``promote_if_better`` only greenlights a candidate whose
                    measured metric actually exceeds the baseline.

Two honesty rules hold throughout:

  * Records whose source PDF cannot be located on disk are SKIPPED and counted —
    never scored against a fabricated re-extraction. The numbers reflect only
    what was really measured.
  * This module *decides*; it does not mutate any prompt. Arithmetic gates (the
    reconcile / tax-key math elsewhere in the pipeline) still decide truth. The
    eval loop only chooses which config earns the right to run.

All LLM work flows through the existing engines (``v11.presto`` / ``v13.scribe``)
which call OpenRouter only — this module adds no new provider. ``score_against_
golden`` makes REAL API calls, so it is on-demand only. Everything here is
fail-safe: DB / IO / extraction failures degrade to a safe empty value and NEVER
raise into a caller.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import database  # type: ignore
    from database import sqlite3  # type: ignore
except Exception:  # pragma: no cover
    database = None  # type: ignore
    sqlite3 = None  # type: ignore

_ARCHIVE_KEY = "learn_eval_archive"
_ARCHIVE_CAP = 50

# Fields treated as numeric (percentage-tolerance match) regardless of the value
# happening to parse as a string — amounts, rates, fees, values.
_NUMERIC_HINTS = (
    "value", "price", "amount", "rate", "duty", "tax", "fee", "cost",
    "customs_value_mmk", "quantity", "exchange_rate", "invoice_price",
    "freight", "insurance", "adjustment",
)


# ---------------------------------------------------------------------------
# DB time helper (no wall-clock nondeterminism — ask the DB)
# ---------------------------------------------------------------------------
def _db_now() -> str:
    """Return ``datetime('now')`` from the DB, or "" on any failure."""
    if database is None:
        return ""
    conn = None
    try:
        conn = database._connect()
        cur = conn.cursor()
        cur.execute("SELECT datetime('now')")
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else ""
    except Exception as exc:  # pragma: no cover
        logger.debug("_db_now failed: %s", exc)
        return ""
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Value normalisation + field matching
# ---------------------------------------------------------------------------
def _to_float(v):
    """Best-effort parse to float. Returns None if not numeric."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            return float(v)
        except Exception:
            return None
    try:
        s = str(v).strip().replace(",", "").replace(" ", "")
        if s == "":
            return None
        return float(s)
    except Exception:
        return None


def _norm_value(v):
    """Normalise a value into a comparable form.

    - numeric strings / numbers -> ``float`` (rounded to 2dp)
    - other strings -> stripped, upper-cased, commas removed
    - None -> None
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        try:
            return round(float(v), 2)
        except Exception:
            return v
    f = _to_float(v)
    if f is not None:
        return round(f, 2)
    try:
        return str(v).strip().replace(",", "").upper()
    except Exception:
        return v


def _is_empty(v) -> bool:
    if v is None:
        return True
    try:
        return str(v).strip() == ""
    except Exception:
        return False


def _is_numeric_field(field, a, b) -> bool:
    """Numeric if the field name hints at a numeric quantity, or both sides
    parse as floats."""
    if field is not None:
        fl = str(field).lower()
        if any(h in fl for h in _NUMERIC_HINTS):
            return True
    return _to_float(a) is not None and _to_float(b) is not None


def field_match(a, b, field=None, tol_pct=1.0) -> bool:
    """True if two values match under the field's comparison rule.

    - both empty/None -> True
    - numeric fields -> match within ``tol_pct`` percent (default 1%)
    - date fields (name contains "date") -> compare ISO yyyy-mm-dd prefix
    - else -> case-insensitive, stripped string equality
    """
    try:
        a_empty, b_empty = _is_empty(a), _is_empty(b)
        if a_empty and b_empty:
            return True
        if a_empty != b_empty:
            return False

        fl = str(field).lower() if field is not None else ""

        # Date fields: compare the yyyy-mm-dd prefix.
        if "date" in fl:
            sa = str(a).strip()[:10]
            sb = str(b).strip()[:10]
            if sa == sb:
                return True
            # fall through to string compare if prefixes differ but strings equal
            return sa.upper() == sb.upper()

        # Numeric fields: percentage tolerance.
        if _is_numeric_field(field, a, b):
            fa, fb = _to_float(a), _to_float(b)
            if fa is None or fb is None:
                # one side non-numeric -> fall back to string equality
                return _norm_value(a) == _norm_value(b)
            if fa == fb:
                return True
            denom = max(abs(fa), abs(fb))
            if denom == 0:
                return True
            return (abs(fa - fb) / denom) * 100.0 <= float(tol_pct)

        # Default: normalised string equality.
        return _norm_value(a) == _norm_value(b)
    except Exception as exc:  # pragma: no cover
        logger.debug("field_match failed (%r vs %r): %s", a, b, exc)
        return False


# ---------------------------------------------------------------------------
# Record-level comparison
# ---------------------------------------------------------------------------
def _item_customs_values(items) -> list:
    """Extract the customs_value_mmk floats from a list of item dicts."""
    out = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        v = it.get("customs_value_mmk")
        if v is None:
            v = it.get("Customs Value (MMK)")
        f = _to_float(v)
        if f is not None:
            out.append(f)
    return out


def _item_recall(truth_items, extracted_items, tol_pct=1.0) -> tuple:
    """Fraction of truth items whose customs_value_mmk is matched by some
    extracted item. Returns ``(recall_float, n_truth_items)``."""
    truth_vals = _item_customs_values(truth_items)
    n_truth = len(truth_vals)
    if n_truth == 0:
        return 1.0, 0
    ext_vals = _item_customs_values(extracted_items)
    remaining = list(ext_vals)
    matched = 0
    for tv in truth_vals:
        hit_idx = None
        for i, ev in enumerate(remaining):
            if field_match(tv, ev, field="customs_value_mmk", tol_pct=tol_pct):
                hit_idx = i
                break
        if hit_idx is not None:
            matched += 1
            remaining.pop(hit_idx)
    return (matched / n_truth if n_truth else 1.0), n_truth


def compare_record(extracted: dict, truth: dict, fields=None) -> dict:
    """Compare one extraction's ``declaration`` dict to the golden declaration.

    Scores only the fields the ground truth actually contains (when ``fields``
    is None, the truth's own keys). Also computes item recall on
    customs_value_mmk.
    """
    result = {
        "per_field": {},
        "matched": 0,
        "scored": 0,
        "accuracy": 0.0,
        "item_recall": 0.0,
        "n_items_truth": 0,
    }
    try:
        extracted = extracted or {}
        truth = truth or {}
        edecl = extracted.get("declaration") if "declaration" in extracted else extracted
        tdecl = truth.get("declaration") if "declaration" in truth else truth
        edecl = edecl or {}
        tdecl = tdecl or {}

        if fields is None:
            fields = list(tdecl.keys())

        per_field = {}
        matched = 0
        for f in fields:
            ok = field_match(edecl.get(f), tdecl.get(f), field=f)
            per_field[f] = bool(ok)
            if ok:
                matched += 1
        scored = len(fields)
        result["per_field"] = per_field
        result["matched"] = matched
        result["scored"] = scored
        result["accuracy"] = (matched / scored) if scored else 1.0

        recall, n_truth = _item_recall(truth.get("items"), extracted.get("items"))
        result["item_recall"] = recall
        result["n_items_truth"] = n_truth
    except Exception as exc:  # pragma: no cover
        logger.debug("compare_record failed: %s", exc)
    return result


def aggregate(records: list) -> dict:
    """Aggregate a list of per-record compare dicts into corpus-level metrics."""
    out = {
        "n": 0,
        "field_accuracy": 0.0,
        "item_recall": 0.0,
        "per_field": {},
    }
    try:
        records = records or []
        n = len(records)
        out["n"] = n
        if n == 0:
            return out

        acc_sum = 0.0
        recall_sum = 0.0
        field_acc = {}  # field -> [matched_count, n]
        for r in records:
            r = r or {}
            acc_sum += float(r.get("accuracy", 0.0) or 0.0)
            recall_sum += float(r.get("item_recall", 0.0) or 0.0)
            for f, ok in (r.get("per_field") or {}).items():
                slot = field_acc.setdefault(f, [0, 0])
                slot[1] += 1
                if ok:
                    slot[0] += 1

        out["field_accuracy"] = acc_sum / n
        out["item_recall"] = recall_sum / n
        out["per_field"] = {
            f: {"acc": (m / c if c else 0.0), "n": c}
            for f, (m, c) in field_acc.items()
        }
    except Exception as exc:  # pragma: no cover
        logger.debug("aggregate failed: %s", exc)
    return out


# ---------------------------------------------------------------------------
# PDF location
# ---------------------------------------------------------------------------
def _locate_pdf(record) -> Optional[str]:
    """Best-effort find the source PDF for a golden record on disk.

    Searches ``config.UPLOAD_FOLDER`` for a file whose name contains the record's
    ``pdf_hash`` or equals / contains its ``pdf_name``. Returns an absolute path
    or None.
    """
    try:
        record = record or {}
        pdf_hash = (record.get("pdf_hash") or "").strip()
        pdf_name = (record.get("pdf_name") or "").strip()
        if not pdf_hash and not pdf_name:
            return None
        try:
            import config  # lazy — module must import without config present
            folder = getattr(config, "UPLOAD_FOLDER", None)
        except Exception:
            folder = None
        if folder is None:
            return None

        from pathlib import Path
        folder = Path(folder)
        if not folder.exists():
            return None

        # 1) exact name match
        if pdf_name:
            cand = folder / pdf_name
            if cand.exists() and cand.is_file():
                return str(cand.resolve())

        # 2) scan for hash / name substring
        name_l = pdf_name.lower()
        for p in folder.iterdir():
            try:
                if not p.is_file():
                    continue
                nm = p.name
                if pdf_hash and pdf_hash in nm:
                    return str(p.resolve())
                if pdf_name and (name_l in nm.lower() or nm.lower() in name_l):
                    return str(p.resolve())
            except Exception:
                continue
        return None
    except Exception as exc:  # pragma: no cover
        logger.debug("_locate_pdf failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Orchestrator — real re-extraction + scoring
# ---------------------------------------------------------------------------
def score_against_golden(engine="presto", limit=None, importer_name=None,
                         model=None) -> dict:
    """Re-extract every locatable golden PDF and score it against approved truth.

    Makes REAL OpenRouter calls (via the existing engines) — on-demand only.
    Records with no locatable PDF are SKIPPED and counted honestly. Never raises.
    """
    safe = {
        "n": 0, "field_accuracy": 0.0, "item_recall": 0.0, "per_field": {},
        "engine": engine, "model": model, "skipped": 0, "evaluated": 0,
        "cost": 0.0, "golden_count": 0,
    }
    try:
        from v11.learn import golden
        corpus = golden.build_golden(limit=limit)
        golden_count = corpus.get("count", 0)
        records = corpus.get("records", []) or []

        compares = []
        skipped = 0
        evaluated = 0
        total_cost = 0.0

        for rec in records:
            pdf_path = _locate_pdf(rec)
            if not pdf_path:
                skipped += 1
                continue
            try:
                out = None
                if engine == "presto":
                    from v11 import presto
                    out = presto.run(pdf_path, model=model,
                                     importer_name=importer_name)
                elif engine == "scribe":
                    from v13 import scribe
                    out = scribe.run(pdf_path, importer_name=importer_name)
                else:
                    logger.debug("unknown engine %r; skipping", engine)
                    skipped += 1
                    continue

                if not out:
                    skipped += 1
                    continue

                # cost accumulation (spec: out.get("cost"); tolerate cost_usd)
                c = out.get("cost")
                if c is None:
                    c = out.get("cost_usd")
                cf = _to_float(c)
                if cf is not None:
                    total_cost += cf

                extracted = {
                    "declaration": out.get("declaration") or {},
                    "items": out.get("items") or [],
                }
                truth = {
                    "declaration": rec.get("declaration") or {},
                    "items": rec.get("items") or [],
                }
                compares.append(compare_record(extracted, truth))
                evaluated += 1
            except Exception as exc:
                logger.debug("re-extract/score failed for %s: %s",
                             rec.get("pdf_name"), exc)
                skipped += 1
                continue

        agg = aggregate(compares)
        agg.update({
            "engine": engine,
            "model": model,
            "skipped": skipped,
            "evaluated": evaluated,
            "cost": round(total_cost, 6),
            "golden_count": golden_count,
        })
        return agg
    except Exception as exc:  # pragma: no cover
        logger.debug("score_against_golden failed: %s", exc)
        return safe


# ---------------------------------------------------------------------------
# Scored archive (settings kv store)
# ---------------------------------------------------------------------------
def list_scores() -> list:
    """Return the archived score list (newest first), or [] on any error."""
    if database is None:
        return []
    try:
        raw = database.get_setting(_ARCHIVE_KEY)
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception as exc:  # pragma: no cover
        logger.debug("list_scores failed: %s", exc)
        return []


def save_score(label: str, metrics: dict, by: str = "system") -> dict:
    """Append a scored entry to the archive (newest first, capped). Returns the
    stored entry (or a safe echo on error — never raises)."""
    entry = {"label": label, "metrics": metrics or {}, "at": _db_now()}
    if database is None:
        return entry
    try:
        archive = list_scores()
        archive.insert(0, entry)
        archive = archive[:_ARCHIVE_CAP]
        database.set_setting(_ARCHIVE_KEY, json.dumps(archive, default=str), by)
    except Exception as exc:  # pragma: no cover
        logger.debug("save_score failed: %s", exc)
    return entry


def best_score(metric="field_accuracy") -> Optional[dict]:
    """Return the archive entry with the highest ``metrics[metric]``, or None."""
    try:
        archive = list_scores()
        best = None
        best_val = None
        for e in archive:
            try:
                v = _to_float((e.get("metrics") or {}).get(metric))
            except Exception:
                v = None
            if v is None:
                continue
            if best_val is None or v > best_val:
                best_val = v
                best = e
        return best
    except Exception as exc:  # pragma: no cover
        logger.debug("best_score failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Promotion gate — measured lift decides adoption
# ---------------------------------------------------------------------------
def promote_if_better(candidate_label: str, candidate_metrics: dict,
                      baseline_metrics: dict = None,
                      metric="field_accuracy") -> dict:
    """Decide whether a candidate beats the baseline on ``metric``.

    Baseline defaults to the best archived score's metrics (or 0). On a genuine
    win the candidate is archived via ``save_score``. Pure decision + archive —
    it changes no prompt itself. Never raises.
    """
    result = {"promoted": False, "delta": 0.0, "candidate": 0.0, "baseline": 0.0}
    try:
        candidate_metrics = candidate_metrics or {}
        cand = _to_float(candidate_metrics.get(metric)) or 0.0

        if baseline_metrics is None:
            best = best_score(metric)
            baseline_metrics = (best or {}).get("metrics") if best else None
        base = _to_float((baseline_metrics or {}).get(metric)) or 0.0

        delta = cand - base
        promoted = delta > 0.0

        result["candidate"] = cand
        result["baseline"] = base
        result["delta"] = round(delta, 6)
        result["promoted"] = bool(promoted)

        if promoted:
            save_score(candidate_label, candidate_metrics, by="promote")
    except Exception as exc:  # pragma: no cover
        logger.debug("promote_if_better failed: %s", exc)
    return result


# CLI: python -m v11.learn.evaluate [engine] [limit]
if __name__ == "__main__":  # pragma: no cover
    import sys
    _engine = sys.argv[1] if len(sys.argv) > 1 else "presto"
    _limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    _res = score_against_golden(engine=_engine, limit=_limit)
    print(json.dumps(_res, indent=2, ensure_ascii=False, default=str))
