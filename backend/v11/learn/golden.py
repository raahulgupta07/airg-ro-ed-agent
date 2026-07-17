#!/usr/bin/env python3
"""LEARNER — golden corpus from approved jobs (Phase-6 self-improvement).

The original golden PDF corpus was lost, which blocks regression tests and model
bake-offs. But every human-APPROVED job is, by definition, a labelled example:
its stored declaration + items are the reviewer-verified ground truth. This
module reconstructs a golden set from those approvals — so the corpus rebuilds
itself from real production review instead of being hand-curated.

Output shape (JSON-serialisable)::

    {
      "count": N,
      "records": [
        {"job_id","pdf_name","pdf_hash",
         "declaration": {field: value, ...},
         "items": [ {field: value, ...}, ... ]}
      ]
    }

`pdf_hash` lets a bake-off re-locate the source PDF if it is still on disk/S3.
Read-only; never raises → returns an empty corpus on any error.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import database  # type: ignore
except Exception:  # pragma: no cover
    database = None  # type: ignore

# Header fields worth pinning as ground truth (the ones the UAT cares about).
_GOLDEN_DECL_FIELDS = (
    "importer_name", "consignor_name", "declaration_no", "declaration_date",
    "currency", "exchange_rate", "invoice_price", "total_customs_value",
    "import_export_customs_duty", "commercial_tax_ct", "advance_income_tax_at",
    "security_fee_sf", "maccs_service_fee_mf",
    "invoice_number", "invoice_number_customs_declaration",
    "invoice_number_commercial_invoice",
    "freight_value", "insurance_value", "adjustment_value",
)


def build_golden(limit: Optional[int] = None) -> dict:
    """Assemble the golden corpus from approved jobs. ``{"count":0,"records":[]}``
    on empty/error."""
    if database is None:
        return {"count": 0, "records": []}
    try:
        jobs = database.get_approved_jobs_full(limit=limit)
    except Exception as exc:
        logger.debug("build_golden failed: %s", exc)
        return {"count": 0, "records": []}

    records = []
    for j in jobs:
        decl = j.get("declaration") or {}
        # keep only the golden fields that are actually present + non-empty
        gdecl = {k: decl[k] for k in _GOLDEN_DECL_FIELDS
                 if k in decl and decl[k] not in (None, "")}
        records.append({
            "job_id": j.get("job_id"),
            "pdf_name": j.get("pdf_name"),
            "pdf_hash": j.get("pdf_hash"),
            "declaration": gdecl,
            "items": j.get("items") or [],
        })
    return {"count": len(records), "records": records}


def export_json(path: str, limit: Optional[int] = None) -> int:
    """Write the golden corpus to ``path`` as JSON. Returns record count."""
    corpus = build_golden(limit=limit)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(corpus, fh, indent=2, ensure_ascii=False, default=str)
    return corpus.get("count", 0)


# CLI: python -m v11.learn.golden [out.json] [limit]
if __name__ == "__main__":  # pragma: no cover
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "golden_truth.json"
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else None
    n = export_json(out, lim)
    print(f"wrote {n} golden records → {out}")
