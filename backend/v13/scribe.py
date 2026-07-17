"""V13 "Scribe" — handwriting / scanned-ink extractor (standalone, image-based).

Pipeline for pages with NO text layer (handwritten or scanned-typed):
  1. render each page at high DPI
  2. ask a strong vision model for declaration + items as strict JSON, N times
     (temperature > 0) — self-consistency
  3. vote per field across the N reads; for items, keep the read whose customs
     values best match the voted declared total
  4. arithmetic gates (item-sum + CIF closure via v11.tools.reconcile) → mark
     needs_review when the math doesn't close (handwriting errors usually do)

Returns a V7-shaped dict so it can drop into the same review UI / save path.
Changes NO existing pipeline; nothing routes here unless a caller invokes run().
"""
import base64
import io
import json
import re
import time
from collections import Counter
from typing import Dict, List, Optional

import fitz
import requests
from PIL import Image

import v13.config as cfg
from v11.presto_schema import PrestoResult           # reuse schema (read-only)
from v11.tools import reconcile as _rc                # reuse gates (read-only)

API_URL = "https://openrouter.ai/api/v1/chat/completions"

PROMPT = """This is a HANDWRITTEN or scanned Myanmar customs document. Read the
ink/printed values carefully. Extract the declaration header and every line item
into strict JSON:

{
  "declaration": {"declaration_no","declaration_date","importer_name",
    "consignor_name","invoice_number","invoice_number_customs",
    "invoice_number_commercial","currency","currency_2","exchange_rate",
    "invoice_price","freight_value","insurance_value","adjustment_value",
    "total_customs_value","customs_duty","commercial_tax",
    "advance_income_tax","security_fee","maccs_service_fee","exemption"},
  "items": [{"item_name","hs_code","quantity","invoice_unit_price",
    "cif_unit_price","customs_value_mmk","customs_duty_rate","commercial_tax_pct",
    "origin","currency","exchange_rate"}],
  "document_format": "CUSDEC1" or "MACCS",
  "field_confidence": 0.0-1.0
}

Rules:
- Numbers as numbers (strip separators); rates as fractions (15%->0.15).
- origin as ISO-2 code (ITALY->IT). quantity keeps its unit ("108 KG").
- freight_value / insurance_value / adjustment_value: in the INVOICE currency (not
  MMK); 0 if not shown. adjustment_value is signed (negative for a deduction).
- exchange_rate: (invoice_price + freight_value + insurance_value + adjustment_value)
  x exchange_rate should be ~ total_customs_value.
- If a handwritten digit is ambiguous, give your best reading and lower
  field_confidence. Return ONLY the JSON object.
"""


def _render(pdf_path: str, pages: Optional[List[int]], dpi: int) -> List[str]:
    out = []
    doc = fitz.open(str(pdf_path))
    want = set(pages) if pages else None
    for i, page in enumerate(doc, 1):
        if want and i not in want:
            continue
        pix = page.get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        if max(img.size) > 2200:
            img.thumbnail((2200, 2200), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        out.append(base64.b64encode(buf.getvalue()).decode())
    doc.close()
    return out


def _parse_json(raw: str):
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    s = s.strip().rstrip("`").strip()
    if "{" not in s:
        return None
    s = s[s.index("{"):s.rindex("}") + 1]
    for cand in (s, re.sub(r",(\s*[\}\]])", r"\1", s)):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def _primary_hints(importer_name: Optional[str]) -> str:
    """Flag-gated learned-correction hint block (LEARN_FEWSHOT_PRIMARY). Never
    raises — an inert/empty learner degrades to no injection."""
    try:
        from v11.learn import fewshot
        return fewshot.primary_hint_block(importer_name) or ""
    except Exception:
        return ""


def _call_vlm(images: List[str], temperature: float, model: str = None,
              prompt: str = None) -> Dict:
    parts = [{"type": "text", "text": prompt or PROMPT}]
    for b64 in images:
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    payload = {"model": model or cfg.SCRIBE_MODEL,
               "messages": [{"role": "user", "content": parts}],
               "temperature": temperature, "max_tokens": 8000}
    usage = {}
    for attempt in range(3):
        try:
            r = requests.post(API_URL, headers={
                "Authorization": f"Bearer {cfg.OPENROUTER_API_KEY}",
                "Content-Type": "application/json"}, json=payload, timeout=180)
            if r.status_code == 200:
                body = r.json()
                raw = body["choices"][0]["message"]["content"]
                usage = body.get("usage", {}) or {}
                return {"parsed": _parse_json(raw) or {}, "usage": usage}
            if r.status_code == 429:
                time.sleep(2 ** (attempt + 1)); continue
        except Exception:
            pass
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return {"parsed": {}, "usage": usage}


def _norm(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


_ITEM_PROMPT = """This is a Myanmar customs declaration page. Extract EVERY line
item visible in the goods table into strict JSON — do not skip or merge rows:
{"items": [{"item_name","hs_code","quantity","customs_value_mmk",
  "customs_duty_rate","commercial_tax_pct","origin","invoice_unit_price"}]}
Numbers as numbers (strip separators); rates as fractions (15%->0.15); origin as
ISO-2 code. customs_value_mmk is the item TOTAL customs value in MMK (the larger
MMK number), NOT the per-unit MMK price. Return ONLY the JSON object."""


def _recover_items(pdf_path: str, pages: Optional[List[int]]) -> list:
    """Verifier-lite for ink/scan: re-read each page at higher DPI with a
    focused item-only prompt and collect all line items. Used when the first
    vote produced 0 items or an unbalanced sum (e.g. dense scanned forms the
    holistic read missed). Best-effort; returns [] on failure.
    """
    imgs = _render(pdf_path, pages, min(cfg.SCRIBE_DPI + 100, 450))
    found = []
    for b64 in imgs:
        res = _call_vlm_one(b64, _ITEM_PROMPT)
        for it in (res.get("items") or []):
            cv = _rc._to_float(it.get("customs_value_mmk"))
            if cv and cv > 0:
                found.append(it)
    return found


def _call_vlm_one(b64: str, prompt: str) -> dict:
    parts = [{"type": "text", "text": prompt},
             {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]
    payload = {"model": cfg.SCRIBE_MODEL, "messages": [{"role": "user", "content": parts}],
               "temperature": 0, "max_tokens": 4000}
    for attempt in range(2):
        try:
            r = requests.post(API_URL, headers={
                "Authorization": f"Bearer {cfg.OPENROUTER_API_KEY}",
                "Content-Type": "application/json"}, json=payload, timeout=120)
            if r.status_code == 200:
                return _parse_json(r.json()["choices"][0]["message"]["content"]) or {}
        except Exception:
            pass
        time.sleep(1.5)
    return {}


def _dedup_items(items: list) -> list:
    seen, out = set(), []
    for it in items or []:
        hs = str(it.get("hs_code") or "").replace(" ", "")
        cv = _rc._to_float(it.get("customs_value_mmk"))
        k = f"{hs}|{int(cv) if cv else ''}|{str(it.get('item_name') or '')[:20].upper()}"
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def _vote_decl(reads: List[PrestoResult]) -> (dict, dict):
    """Majority-vote each declaration field; return (decl, field_confidence)."""
    decl, conf = {}, {}
    keys = PrestoResult().declaration.model_dump().keys()
    for k in keys:
        vals = [getattr(r.declaration, k) for r in reads]
        present = [(v, _norm(v)) for v in vals if v not in (None, "")]
        if not present:
            decl[k] = None; conf[k] = 0.0; continue
        c = Counter(n for _, n in present)
        top_norm, top_n = c.most_common(1)[0]
        # pick the original-typed value matching the winning normalized form
        decl[k] = next(v for v, n in present if n == top_norm)
        conf[k] = round(top_n / len(reads), 2)
    return decl, conf


def _best_items(reads: List[PrestoResult], declared_total) -> list:
    """Pick the read whose item customs-value sum best matches declared total;
    tie-break on most items."""
    best, best_score = [], None
    for r in reads:
        items = [it.model_dump() for it in r.items]
        s = sum((_rc._to_float(it.get("customs_value_mmk")) or 0) for it in items)
        if declared_total:
            score = abs(s - declared_total)
        else:
            score = -len(items)  # no anchor → prefer most items
        if best_score is None or score < best_score or (
                score == best_score and len(items) > len(best)):
            best, best_score = items, score
    return best


def run(pdf_path: str, pages: Optional[List[int]] = None,
        importer_name: Optional[str] = None) -> Dict:
    t0 = time.time()
    images = _render(pdf_path, pages, cfg.SCRIBE_DPI)
    n = max(1, cfg.SCRIBE_VOTES)

    # P3 self-improvement: when historically-weak fields exist, spend MORE votes
    # (cross-model reads) to raise agreement exactly where the engine errs most.
    # Flag-gated LEARN_ADAPTIVE_VOTES; importer usually unknown → global weak set.
    try:
        import os as _os
        if str(_os.getenv("LEARN_ADAPTIVE_VOTES", "0")).strip().lower() in ("1", "true", "yes", "on"):
            from v11.learn import weakspots
            _cap = int(_os.getenv("SCRIBE_MAX_VOTES", "5"))
            plan = weakspots.vote_plan(importer_name, base_votes=n, weak_votes=n + 2)
            if plan:
                n = min(_cap, max(n, max(plan.values())))
    except Exception:
        pass

    # Phase-1 self-improvement: prepend learned-correction hints to the vote
    # prompt (flag-gated LEARN_FEWSHOT_PRIMARY; "" when off). Computed once so
    # every vote sees the same guidance.
    hints = _primary_hints(importer_name)
    vote_prompt = (hints + "\n" + PROMPT) if hints else PROMPT

    reads, ti, to, cost = [], 0, 0, 0.0
    models = cfg.SCRIBE_MODELS or [cfg.SCRIBE_MODEL]
    for i in range(n):
        temp = 0.0 if i == 0 else cfg.SCRIBE_TEMPERATURE
        # Rotate across models → field_confidence = cross-MODEL agreement.
        res = _call_vlm(images, temp, models[i % len(models)], prompt=vote_prompt)
        try:
            reads.append(PrestoResult.model_validate(res["parsed"]))
        except Exception:
            reads.append(PrestoResult())
        u = res["usage"] or {}
        ti += int(u.get("prompt_tokens", 0) or 0)
        to += int(u.get("completion_tokens", 0) or 0)
        if u.get("cost") is not None:
            try:
                cost += float(u["cost"])
            except Exception:
                pass

    decl, conf = _vote_decl(reads)
    declared_total = _rc._to_float(decl.get("total_customs_value"))
    items = _best_items(reads, declared_total)
    doc_fmt = Counter(_norm(r.document_format) for r in reads
                      if r.document_format).most_common(1)
    document_format = doc_fmt[0][0] if doc_fmt else "CUSDEC1"

    verdict = _rc.reconcile(decl, items)
    # Gate-triggered self-correction: fix the broken header field instead of
    # flagging the whole doc (no slow fallback).
    sc_log = []
    if not verdict.get("balanced"):
        try:
            from v11.tools import self_correct as _sc
            cor = _sc.correct(pdf_path, decl, items, verdict,
                              header_page=1, model=cfg.SCRIBE_MODEL)
            if cor.get("corrected"):
                decl = cor["declaration"]
                verdict = cor["verdict"]
                sc_log = cor["log"]
        except Exception:
            pass

    # Verifier-lite: if still unbalanced (esp. 0 items or a sum gap), re-read the
    # item table at higher DPI with a focused item-only prompt and keep whichever
    # set balances best. Targets dense scanned forms the holistic vote missed.
    recover_log = []
    if not verdict.get("balanced"):
        try:
            rec = _dedup_items(_recover_items(pdf_path, pages))
            if rec:
                merged = _dedup_items(items + rec)
                cand = max([rec, merged], key=lambda s: -abs(
                    sum((_rc._to_float(i.get("customs_value_mmk")) or 0) for i in s)
                    - (declared_total or 0)) if declared_total else len(s))
                cv = _rc.reconcile(decl, cand)
                if cv.get("balanced") or len(cand) > len(items):
                    recover_log.append(f"item-recover: {len(items)}→{len(cand)} items")
                    items = cand
                    verdict = cv
        except Exception:
            pass

    low_conf = [k for k, c in conf.items() if c < 0.67]
    needs_review = (not verdict.get("balanced")) or bool(low_conf)

    return {
        "pipeline_version": "scribe",
        "pipeline_mode": "v13_scribe",
        "declaration": decl,
        "items": items,
        "document_format": document_format,
        "items_count": len(items),
        "is_handwritten": True,
        "duration_seconds": round(time.time() - t0, 2),
        "cost_usd": round(cost, 6),
        "cost": round(cost, 6),
        "tokens_in": ti,
        "tokens_out": to,
        "cost_breakdown": [{"step": "scribe_vote", "model": cfg.SCRIBE_MODEL,
                            "input_tokens": ti, "output_tokens": to,
                            "cost": round(cost, 6), "source": "openrouter",
                            "branch": "scribe"}],
        "needs_review": needs_review,
        "scribe": {"votes": n, "field_confidence": conf,
                   "low_confidence_fields": low_conf,
                   "reconcile": verdict, "pages": len(images),
                   "fewshot_injected": bool(hints),
                   "self_correct": sc_log, "item_recover": recover_log},
    }


# CLI: python -m v13.scribe <pdf> [page,page,...]
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m v13.scribe <pdf_path> [comma,pages]")
        sys.exit(1)
    pg = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else None
    r = run(sys.argv[1], pg)
    print(json.dumps({k: v for k, v in r.items()
                      if k not in ("cost_breakdown",)}, indent=2,
                     ensure_ascii=False, default=str))
