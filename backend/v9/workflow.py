"""V9 — Extraction workflow.
Sequential pipeline: filter → reader → verifier → deterministic post-fix.
Master agent is available but not invoked for MVP (kept simple, predictable).
"""
import time
from pathlib import Path
from typing import Dict

from v9.agents.filter import filter_pages
from v9.agents.reader import read_pdf, flatten
from v9.agents.verifier import verify, apply_corrections
from v9.tools.pdf_tools import slice_pdf as slice_pdf_tool
from v9.tools.math_tools import deterministic_postfix
from v9.tools.db_tools import consignor_canonical, hs_lookup


def _direct_slice(pdf_path: str, pages: list) -> str:
    """Bypass Agno tool wrapper to call the slice helper directly."""
    import os, fitz
    src = fitz.open(pdf_path)
    dst = fitz.open()
    for p in sorted(set(pages)):
        if 1 <= p <= len(src):
            dst.insert_pdf(src, from_page=p - 1, to_page=p - 1)
    out_dir = os.environ.get("V9_TMP_DIR", "/tmp/v9_results")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"_sliced_{Path(pdf_path).stem}.pdf")
    dst.save(out)
    dst.close()
    src.close()
    return out


def _direct_postfix(decl: dict) -> dict:
    """Pure-rule application without going through Agno tool."""
    def _f(x):
        if x in (None, ""): return 0.0
        try: return float(str(x).replace(",", ""))
        except Exception: return 0.0
    notes = []
    cv = _f(decl.get("total_customs_value"))
    duty = _f(decl.get("customs_duty"))
    if cv > 0:
        mf = _f(decl.get("maccs_service_fee"))
        if mf == 0:
            decl["maccs_service_fee"] = round(cv * 0.02)
            notes.append(f"MACCS = 2% × {int(cv)} = {round(cv*0.02)}")
        ct = _f(decl.get("commercial_tax"))
        if ct == 0:
            decl["commercial_tax"] = round((cv + duty) * 0.05)
            notes.append("CT = 5% × (CV+Duty)")
    return notes


def run(pdf_path: str) -> Dict:
    """End-to-end extraction. Returns ExtractionResult-shaped dict."""
    t0 = time.time()
    out = {
        "pdf": pdf_path,
        "document_format": None,
        "declaration": {},
        "items": [],
        "pages_kept": [],
        "anomalies": [],
        "postfix_notes": [],
        "duration_seconds": 0,
        "cost_usd": 0,  # TODO wire in cost from agent responses
        "trace": [],
    }

    # ─── Phase 1: page filter ───
    try:
        kept, meta = filter_pages(pdf_path)
        out["pages_kept"] = kept
        out["trace"].append({"phase": "filter", "kept": kept, "meta": meta})
        if len(kept) < meta.get("n_pages", 0):
            sliced = _direct_slice(pdf_path, kept)
            work_pdf = sliced
        else:
            work_pdf = pdf_path
    except Exception as e:
        out["trace"].append({"phase": "filter", "error": str(e)})
        work_pdf = pdf_path

    # ─── Phase 2: holistic read ───
    raw = read_pdf(work_pdf)
    if not raw:
        out["trace"].append({"phase": "reader", "error": "no_result"})
        out["duration_seconds"] = round(time.time() - t0, 1)
        return out
    out["trace"].append({"phase": "reader", "ok": True,
                          "uncertain_fields": raw.get("uncertain_fields", [])})

    # ─── Phase 3: verifier corrections ───
    verifier_out = verify(work_pdf, raw)
    if verifier_out:
        raw = apply_corrections(raw, verifier_out)
        out["trace"].append({"phase": "verifier", "ok": True,
                              "n_corrections": len(verifier_out.get("corrections") or {})})
    else:
        out["trace"].append({"phase": "verifier", "skipped": True})

    # ─── Phase 4: flatten ───
    flat = flatten(raw)
    out["document_format"] = flat.get("document_format")
    out["declaration"] = flat.get("declaration") or {}
    out["items"] = flat.get("items") or []
    out["anomalies"] = flat.get("anomalies") or []

    # ─── Phase 4b: decl-no zoom (400 DPI Opus re-read) ───
    try:
        from v9.agents.decl_zoom import zoom_declaration_no, maybe_override
        zoom_res = zoom_declaration_no(work_pdf, out["declaration"].get("declaration_no") or "")
        if zoom_res:
            out["declaration"], override_note = maybe_override(out["declaration"], zoom_res)
            out["trace"].append({"phase": "decl_zoom", "ok": True,
                                  "matches_draft": zoom_res.get("matches_draft"),
                                  "zoom_value": zoom_res.get("value"),
                                  "confidence": zoom_res.get("confidence"),
                                  "override": override_note})
        else:
            out["trace"].append({"phase": "decl_zoom", "skipped": True})
    except Exception as e:
        out["trace"].append({"phase": "decl_zoom", "error": str(e)})

    # ─── Phase 5: deterministic post-fix ───
    notes = _direct_postfix(out["declaration"])
    # If decl_zoom overrode, append note
    for t in out["trace"]:
        if t.get("phase") == "decl_zoom" and t.get("override"):
            notes.append(t["override"])
    # consignor canonical
    cn = out["declaration"].get("consignor_name") or ""
    imp = out["declaration"].get("importer_name") or ""
    if cn and imp:
        try:
            r = consignor_canonical.entrypoint(name=cn, importer=imp) \
                if hasattr(consignor_canonical, "entrypoint") \
                else None
        except Exception:
            r = None
        # Best-effort fallback: read JSON directly
        if not r:
            import json
            from pathlib import Path as _P
            p = _P(__file__).parent / "knowledge" / "consignors.json"
            try:
                seeds = json.loads(p.read_text())
                imp_key = next((k for k in seeds if k in imp.upper()), None)
                if imp_key:
                    first = cn.lower().split()[0] if cn.split() else ""
                    for c in seeds[imp_key]:
                        cf = c.lower().split()[0] if c.split() else ""
                        if cf and first and cf == first and c != cn:
                            out["declaration"]["consignor_name"] = c
                            notes.append(f"consignor: '{cn}' → '{c}'")
                            break
            except Exception:
                pass
    out["postfix_notes"] = notes
    out["duration_seconds"] = round(time.time() - t0, 1)
    return out


# CLI entry: python -m v9.workflow PDF_PATH
if __name__ == "__main__":
    import json, sys
    if len(sys.argv) < 2:
        print("Usage: python -m v9.workflow <pdf_path>")
        sys.exit(1)
    result = run(sys.argv[1])
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
