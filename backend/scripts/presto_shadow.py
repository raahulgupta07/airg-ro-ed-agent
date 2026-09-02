"""V12 Presto — shadow comparison harness (Phase 4, offline, no live impact).

Runs the current V7 pipeline AND the Presto fast-path on the same PDFs, then
reports per-doc and aggregate differences: item counts, key declaration fields,
reconcile balance, time, and cost. Use this on a sample of REAL production PDFs
to decide whether to enable PRESTO_ENABLED — it proves accuracy ≥ V7 before any
routing change.

Usage (inside the app/worker container, real OPENROUTER_API_KEY in env):
    python -m scripts.presto_shadow /path/to/pdf_dir            # all PDFs in dir
    python -m scripts.presto_shadow a.pdf b.pdf c.pdf           # explicit files
    python -m scripts.presto_shadow /dir --json out.json        # also write JSON

Nothing is written to the database; both pipelines run read-only.
"""
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numeric  # noqa: E402  (needs the backend root on the path first)

# Key declaration fields compared field-by-field.
_DECL_KEYS = [
    "declaration_no", "importer_name", "consignor_name", "exchange_rate",
    "invoice_price", "total_customs_value", "customs_duty", "commercial_tax",
    "advance_income_tax", "maccs_service_fee",
]


def _num(v):
    # Shared parser — the bench scorer compared amounts by string-stripping
    # commas, so any value printed with its currency scored as a mismatch
    # against an identical value that happened to lack one.
    return numeric.to_float(v)


def _eq(a, b):
    """Loose equality: numbers within 0.5%, strings case-insensitive trim."""
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        if na == 0 and nb == 0:
            return True
        denom = max(abs(na), abs(nb), 1e-9)
        return abs(na - nb) / denom <= 0.005
    return str(a or "").strip().lower() == str(b or "").strip().lower()


def _items_sum(items):
    s = 0.0
    for it in items or []:
        v = _num(it.get("customs_value_mmk"))
        if v:
            s += v
    return round(s, 2)


def _run_one(pdf_path):
    from pipeline.pipeline import run_pipeline
    from v11 import presto
    # Normalize V7's Title-case declaration keys → snake_case (same keys Presto
    # uses) so the field-by-field diff is apples-to-apples.
    from v11.agents.merger import merge_results

    rec = {"pdf": os.path.basename(pdf_path)}

    # V7
    t = time.time()
    try:
        v7_raw = run_pipeline(pdf_path)
        v7 = merge_results(v7_raw, None)  # snake_case declaration + items
        rec["v7"] = {
            "ok": True, "secs": round(time.time() - t, 1),
            "cost": round(float(v7_raw.get("cost_usd") or v7_raw.get("cost") or 0), 4),
            "items": len(v7.get("items") or []),
            "decl": v7.get("declaration") or {},
            "items_sum": _items_sum(v7.get("items")),
            "total": _num((v7.get("declaration") or {}).get("total_customs_value")),
        }
    except Exception as e:
        rec["v7"] = {"ok": False, "error": str(e)[:200], "secs": round(time.time() - t, 1)}

    # Presto
    t = time.time()
    try:
        pr = presto.run(pdf_path)
        rec["presto"] = {
            "ok": True, "secs": round(time.time() - t, 1),
            "cost": round(float(pr.get("cost_usd") or 0), 4),
            "items": len(pr.get("items") or []),
            "decl": pr.get("declaration") or {},
            "items_sum": _items_sum(pr.get("items")),
            "total": _num((pr.get("declaration") or {}).get("total_customs_value")),
        }
    except Exception as e:
        rec["presto"] = {"ok": False, "error": str(e)[:200], "secs": round(time.time() - t, 1)}

    # Diff
    v7, pr = rec.get("v7", {}), rec.get("presto", {})
    if v7.get("ok") and pr.get("ok"):
        field_diffs = []
        for k in _DECL_KEYS:
            if not _eq(v7["decl"].get(k), pr["decl"].get(k)):
                field_diffs.append({"field": k, "v7": v7["decl"].get(k), "presto": pr["decl"].get(k)})
        rec["diff"] = {
            "item_count_match": v7["items"] == pr["items"],
            "v7_items": v7["items"], "presto_items": pr["items"],
            "decl_field_mismatches": field_diffs,
            "v7_balanced": v7["total"] is not None and abs((v7["total"] or 0) - v7["items_sum"]) / max(abs(v7["total"] or 1), 1) <= 0.05,
            "presto_balanced": pr["total"] is not None and abs((pr["total"] or 0) - pr["items_sum"]) / max(abs(pr["total"] or 1), 1) <= 0.05,
            "speedup_x": round((v7["secs"] or 0) / pr["secs"], 1) if pr.get("secs") else None,
            "cost_ratio": round((v7["cost"] or 0) / pr["cost"], 1) if pr.get("cost") else None,
        }
    return rec


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    json_out = None
    if "--json" in argv:
        i = argv.index("--json")
        json_out = argv[i + 1] if i + 1 < len(argv) else "presto_shadow.json"
        args = [a for a in args if a != json_out]

    pdfs = []
    for a in args:
        if os.path.isdir(a):
            pdfs += sorted(glob.glob(os.path.join(a, "*.pdf")) + glob.glob(os.path.join(a, "*.PDF")))
        elif os.path.isfile(a):
            pdfs.append(a)
    if not pdfs:
        print("No PDFs found. Usage: python -m scripts.presto_shadow <dir|files...> [--json out.json]")
        return 1

    results = []
    agg = {"docs": 0, "item_match": 0, "presto_balanced": 0, "v7_balanced": 0,
           "v7_secs": 0.0, "presto_secs": 0.0, "v7_cost": 0.0, "presto_cost": 0.0,
           "field_mismatch_docs": 0}
    print(f"Shadow comparing {len(pdfs)} PDFs (V7 vs Presto)...\n")
    for p in pdfs:
        r = _run_one(p)
        results.append(r)
        d = r.get("diff")
        v7, pr = r.get("v7", {}), r.get("presto", {})
        if d:
            agg["docs"] += 1
            agg["item_match"] += 1 if d["item_count_match"] else 0
            agg["presto_balanced"] += 1 if d["presto_balanced"] else 0
            agg["v7_balanced"] += 1 if d["v7_balanced"] else 0
            agg["v7_secs"] += v7["secs"]; agg["presto_secs"] += pr["secs"]
            agg["v7_cost"] += v7["cost"]; agg["presto_cost"] += pr["cost"]
            agg["field_mismatch_docs"] += 1 if d["decl_field_mismatches"] else 0
            flag = "OK " if (d["item_count_match"] and not d["decl_field_mismatches"]) else "CHK"
            print(f"[{flag}] {r['pdf']:40s} items v7={d['v7_items']} presto={d['presto_items']} "
                  f"| {v7['secs']}s→{pr['secs']}s ({d['speedup_x']}x) | mismatches={len(d['decl_field_mismatches'])}")
            for fd in d["decl_field_mismatches"]:
                print(f"        ~ {fd['field']}: v7={fd['v7']!r} presto={fd['presto']!r}")
        else:
            print(f"[ERR] {r['pdf']}: v7_ok={v7.get('ok')} presto_ok={pr.get('ok')} "
                  f"{v7.get('error','')}{pr.get('error','')}")

    n = max(agg["docs"], 1)
    print("\n===== AGGREGATE =====")
    print(f"docs compared        : {agg['docs']}")
    print(f"item-count match     : {agg['item_match']}/{agg['docs']}")
    print(f"decl fully matched   : {agg['docs'] - agg['field_mismatch_docs']}/{agg['docs']}")
    print(f"balanced  V7/Presto  : {agg['v7_balanced']}/{agg['docs']}  vs  {agg['presto_balanced']}/{agg['docs']}")
    print(f"avg time  V7/Presto  : {agg['v7_secs']/n:.1f}s  vs  {agg['presto_secs']/n:.1f}s  ({(agg['v7_secs']/max(agg['presto_secs'],1e-9)):.1f}x faster)")
    print(f"total cost V7/Presto : ${agg['v7_cost']:.3f}  vs  ${agg['presto_cost']:.3f}  ({(agg['v7_cost']/max(agg['presto_cost'],1e-9)):.1f}x cheaper)")
    print("\nGo-live gate: item-count match high + decl fully matched high + presto balanced ≥ v7.")

    if json_out:
        with open(json_out, "w") as f:
            json.dump({"results": results, "aggregate": agg}, f, indent=2, default=str)
        print(f"\nWrote {json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
