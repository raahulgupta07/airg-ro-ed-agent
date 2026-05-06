#!/usr/bin/env python3
"""
V8 — Claude-Code-style multi-turn extraction (FastAPI-importable port of cc_extract.py).

All Anthropic models via OpenRouter. 5 passes:
  Pass 0: claude-haiku-4-5   — page filter (declaration vs attachment)
  Pass 1: claude-opus-4.7    — holistic read + self-flagged uncertainty list
  Pass 2: claude-opus-4.7    — re-read uncertain regions (same chat continues)
  Pass 3: claude-sonnet-4-6  — independent cross-vote (fresh chat)
  Pass 4: claude-opus-4.7    — reconciler (sees own + sonnet readings, decides)
  Pass 5: deterministic post-fix (no LLM)

Public entry point: run_one(pdf_path: str) -> dict
"""

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from config import API_KEY

# ─── Config ────────────────────────────────────────────────────────
API_URL = "https://openrouter.ai/api/v1/chat/completions"

OPUS = "anthropic/claude-opus-4.7"
SONNET = "anthropic/claude-sonnet-4-6"
HAIKU = "anthropic/claude-haiku-4-5"

# Optional output/log dirs — default to /tmp inside container, can be overridden.
OUT_DIR = Path(os.getenv("V8_OUT_DIR", "/tmp/v8_results"))
LOG_DIR = Path(os.getenv("V8_LOG_DIR", "/tmp/v8_logs"))
try:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

CCY_BANDS = {
    "THB": (40, 110), "USD": (1300, 5500), "EUR": (1500, 6500),
    "KRW": (1.0, 5.0), "JPY": (10, 50), "CNY": (200, 900),
    "SGD": (1500, 4500), "GBP": (2000, 7000),
}

KNOWN_CONSIGNORS = {
    "PREMIUM DISTRIBUTION": [
        "Asiatic Mart Holding Pte Ltd",
        "Tharikan Foods Co.,Ltd",
        "San Remo Macaroni Company Pty Ltd",
        "Italian Trading Co.,Ltd",
        "Ferrero Asia Pte Ltd",
        "Balducci Foods Pty Ltd",
        "Anchor Food Professionals (Singapore) Pte Ltd",
    ],
}


# ─── Prompts ───────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a meticulous Myanmar Customs document analyst.
Your job: extract every field from CUSDEC-1 / MACCS Release Order PDFs with zero error.

You MUST:
- Read handwritten numbers literally — exact pen-strokes, no inference.
- Validate every number with arithmetic cross-checks: CV÷rate≈InvPrice, qty×unit≈row_total, 15%·CV≈Duty, 5%·(CV+Duty)≈CT, 2%·CV≈MACCS Fee.
- For invoice numbers: the "A - " prefix is the customs-declaration variant; bare form is commercial.
- For Currency 2: if no second currency exists on form, mirror Currency.
- Flag uncertainty explicitly. Better to say "uncertain" than guess.
- Output ONLY valid JSON when asked for JSON. No markdown fences, no preamble.
"""

PASS1_USER = """Read this Myanmar Customs PDF. Extract every field and return JSON below.

For each field include {value, confidence (high|med|low), evidence (max 8 words)}.

Schema:
{
  "document_format": "CUSDEC1|MACCS",
  "declaration": {
    "Declaration No": {...},
    "Declaration Date": {...},   // YYYY-MM-DD
    "Importer (Name)": {...},
    "Consignor (Name)": {...},
    "Invoice Number": {...},
    "Invoice Number (Customs Declaration)": {...},   // with "A - " prefix if present
    "Invoice Number (Commercial Invoice)": {...},    // bare form
    "Currency": {...},                               // 3-letter ISO
    "Currency 2": {...},                             // mirror Currency if no separate
    "Exchange Rate": {...},
    "Invoice Price": {...},
    "Total Customs Value": {...},
    "Country Origin": {...},
    "Import/Export Customs Duty": {...},
    "Commercial Tax (CT)": {...},
    "Advance Income Tax (AT)": {...},
    "Security Fee (SF)": {...},
    "MACCS Service Fee (MF)": {...},
    "Exemption/Reduction": {...}
  },
  "items": [{
    "Item name": {...},
    "Customs duty rate": {...},   // e.g. 0.15, FREE→0, M-40%→0.4
    "Quantity (1)": {...},        // "1234.56 KG"
    "Invoice unit price": {...},
    "CIF unit price": {...},
    "Currency": {...},
    "Commercial tax %": {...},    // 0.05
    "Exchange Rate (1)": {...},
    "HS Code": {...},
    "Origin Country": {...},
    "Customs Value (MMK)": {...}
  }],
  "anomalies": ["..."],
  "uncertain_fields": ["field path that you want to re-examine"]
}

Output strict JSON only. Keep evidence short.
"""

PASS2_USER = """Re-read the PDF carefully ONLY for these fields you flagged as uncertain:
{flagged}

For each, give the corrected reading + new confidence. Return JSON:
{{ "corrections": {{ "field name": {{"value": "...", "confidence": "high|med|low", "evidence": "..."}} }} }}

If you confirm the original was correct, repeat the original value with confidence=high.
"""

PASS4_USER = """Two readings of the same PDF were produced. Reconcile them into the final answer.

Reading A (claude-opus, your refined output):
{a}

Reading B (claude-sonnet, independent):
{b}

For each field where A and B disagree, decide which is correct (re-look at the PDF).
For each field where they agree, keep the agreed value.
Return the COMPLETE final JSON in the same schema as Reading A.
Output JSON only.
"""

PAGE_FILTER_PROMPT = """For each page image label whether it is part of the customs DECLARATION (CUSDEC-1 or MACCS Release Order — the form with boxes 1-44, declaration number, importer/consignor) or an ATTACHMENT (Bill of Lading, Commercial Invoice, Packing List, Certificate of Origin, Truck Receipt, etc).

Return strict JSON:
{"pages": [{"page": 1, "label": "DECL"}, {"page": 2, "label": "ATTACHMENT"}, ...]}

Output JSON only."""


# ─── Helpers ───────────────────────────────────────────────────────
_COST_BUCKET: List[Dict] = []


def _pdf_part(pdf_path: Path) -> Dict:
    b64 = base64.b64encode(pdf_path.read_bytes()).decode()
    return {"type": "file",
            "file": {"filename": pdf_path.name,
                     "file_data": f"data:application/pdf;base64,{b64}"}}


def _post(messages, model, label, timeout=300, max_tokens=32000) -> Optional[Dict]:
    if not API_KEY:
        print(f"  [{label}] missing OPENROUTER_API_KEY")
        return None
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    for attempt in range(3):
        try:
            t0 = time.time()
            r = requests.post(API_URL,
                              headers={"Authorization": f"Bearer {API_KEY}",
                                       "Content-Type": "application/json"},
                              json=payload, timeout=timeout)
            dt = time.time() - t0
            print(f"  [{label}] HTTP {r.status_code} {dt:.1f}s ({model.split('/')[-1]})")
            if r.status_code == 200:
                j = r.json()
                u = j.get("usage", {})
                cost = u.get("cost") or u.get("total_cost") or 0
                _COST_BUCKET.append({"label": label, "model": model, "cost": cost,
                                     "in": u.get("prompt_tokens"),
                                     "out": u.get("completion_tokens")})
                return j
            if r.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            print(f"  [{label}] err: {r.text[:200]}")
        except Exception as e:
            print(f"  [{label}] exc: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return None


def _parse_json(raw: str) -> Optional[Dict]:
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    s = s.strip()
    if s.endswith("```"):
        s = s[:-3].strip()
    if "{" not in s:
        return None
    s = s[s.index("{"):s.rindex("}") + 1]
    for cand in [
        s,
        re.sub(r"(\}\s*)\n(\s*\"[^\"]+\"\s*:)", r"\1,\n\2", s),
        re.sub(r"([\d\"\}\]])\s*\n(\s*\"[^\"]+\"\s*:)", r"\1,\n\2", s),
        re.sub(r",(\s*[\}\]])", r"\1", s),
    ]:
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def _flatten(parsed: Dict) -> Dict:
    """Flatten {value, confidence, evidence} dicts into plain values."""
    fmt = parsed.get("document_format")
    if isinstance(fmt, dict):
        fmt = fmt.get("value")
    out = {
        "document_format": fmt,
        "declaration": {},
        "items": [],
        "anomalies": parsed.get("anomalies", []),
        "uncertain_fields": parsed.get("uncertain_fields", []),
    }
    for k, v in (parsed.get("declaration") or {}).items():
        out["declaration"][k] = v.get("value") if isinstance(v, dict) else v
    for it in (parsed.get("items") or []):
        out["items"].append({
            k: (v.get("value") if isinstance(v, dict) else v)
            for k, v in it.items()
        })
    out["raw"] = parsed
    return out


# ─── Pass 0 — Page filter (Haiku) ──────────────────────────────────
def _render_thumbnails(pdf_path: Path, dpi: int = 60) -> List[str]:
    """Return base64 jpeg thumbnails per page."""
    import io
    import fitz
    from PIL import Image
    out = []
    doc = fitz.open(str(pdf_path))
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        max_dim = 800
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70, optimize=True)
        out.append(base64.b64encode(buf.getvalue()).decode())
    doc.close()
    return out


def pass0_filter(pdf_path: Path) -> Tuple[Path, List[int]]:
    """Returns (sliced_pdf_path, list of kept page indices 1-based)."""
    import fitz
    thumbs = _render_thumbnails(pdf_path)
    n = len(thumbs)
    if n <= 3:
        return pdf_path, list(range(1, n + 1))

    src_doc = fitz.open(str(pdf_path))
    forced = set()
    for i, page in enumerate(src_doc, 1):
        t = (page.get_text() or "").upper()
        if any(kw in t for kw in (
            "DECLARATION NO", "RELEASE ORDER", "CUSDEC",
            "BOX 11", "TOTAL CUSTOMS VALUE", "TOTAL ITEMS"
        )):
            forced.add(i)
    src_doc.close()
    content = []
    for i, b in enumerate(thumbs, 1):
        content.append({"type": "text", "text": f"Page {i}:"})
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b}"}})
    content.append({"type": "text", "text": PAGE_FILTER_PROMPT})
    msgs = [{"role": "user", "content": content}]
    res = _post(msgs, HAIKU, "Pass0", max_tokens=2000)
    if not res:
        return pdf_path, list(range(1, n + 1))
    parsed = _parse_json(res["choices"][0]["message"]["content"])
    if not parsed or "pages" not in parsed:
        return pdf_path, list(range(1, n + 1))
    keep = sorted({p["page"] for p in parsed["pages"] if p.get("label") == "DECL"} | forced | {1})
    if not keep:
        return pdf_path, list(range(1, n + 1))
    src = fitz.open(str(pdf_path))
    dst = fitz.open()
    for k in keep:
        dst.insert_pdf(src, from_page=k - 1, to_page=k - 1)
    out_pdf = OUT_DIR / f"_sliced_{pdf_path.stem}.pdf"
    dst.save(str(out_pdf))
    dst.close()
    src.close()
    return out_pdf, keep


# ─── Pass 1 — Holistic Opus ────────────────────────────────────────
def pass1(pdf_path: Path) -> Optional[Dict]:
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": [_pdf_part(pdf_path),
                                     {"type": "text", "text": PASS1_USER}]},
    ]
    res = _post(msgs, OPUS, "Pass1")
    if not res:
        return None
    raw = res["choices"][0]["message"]["content"]
    parsed = _parse_json(raw)
    if not parsed:
        print(f"  [Pass1] JSON parse fail. raw[:300]: {raw[:300]}")
    return parsed


# ─── Pass 2 — Same chat: re-read flagged ───────────────────────────
def pass2(pdf_path: Path, pass1_raw_json: Dict) -> Optional[Dict]:
    flagged = pass1_raw_json.get("uncertain_fields", [])
    if not flagged:
        return None
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": [_pdf_part(pdf_path),
                                     {"type": "text", "text": PASS1_USER}]},
        {"role": "assistant",
         "content": json.dumps(pass1_raw_json, ensure_ascii=False)[:18000]},
        {"role": "user", "content": [
            _pdf_part(pdf_path),
            {"type": "text",
             "text": PASS2_USER.format(flagged=json.dumps(flagged))}
        ]},
    ]
    res = _post(msgs, OPUS, "Pass2")
    if not res:
        return None
    return _parse_json(res["choices"][0]["message"]["content"])


def apply_corrections(pass1_raw: Dict, corrections_obj: Dict) -> Dict:
    if not corrections_obj:
        return pass1_raw
    cor = corrections_obj.get("corrections", {})
    decl = pass1_raw.get("declaration", {})
    for field, fix in cor.items():
        if field in decl and isinstance(decl[field], dict) and isinstance(fix, dict):
            decl[field] = fix
    return pass1_raw


# ─── Pass 3 — Sonnet independent cross-vote ────────────────────────
def pass3(pdf_path: Path) -> Optional[Dict]:
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": [_pdf_part(pdf_path),
                                     {"type": "text", "text": PASS1_USER}]},
    ]
    res = _post(msgs, SONNET, "Pass3")
    if not res:
        return None
    return _parse_json(res["choices"][0]["message"]["content"])


# ─── Pass 4 — Opus reconciler ──────────────────────────────────────
def pass4(pdf_path: Path, a: Dict, b: Dict) -> Optional[Dict]:
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": [
            _pdf_part(pdf_path),
            {"type": "text",
             "text": PASS4_USER.format(
                 a=json.dumps(a, ensure_ascii=False)[:14000],
                 b=json.dumps(b, ensure_ascii=False)[:14000])}
        ]},
    ]
    res = _post(msgs, OPUS, "Pass4")
    if not res:
        return None
    return _parse_json(res["choices"][0]["message"]["content"])


# ─── Pass 5 — Deterministic post-fix ───────────────────────────────
def pass5(d: Dict) -> Dict:
    decl = d.get("declaration", {})
    items = d.get("items", [])
    notes: List[str] = []

    imp = (decl.get("Importer (Name)") or "").upper()
    cons = decl.get("Consignor (Name)") or ""
    for key, canon_list in KNOWN_CONSIGNORS.items():
        if key in imp and cons:
            for canon in canon_list:
                cw, kw = canon.lower().split(), cons.lower().split()
                if cw and kw and cw[0] == kw[0] and cons != canon:
                    notes.append(f"consignor: '{cons}' → '{canon}'")
                    decl["Consignor (Name)"] = canon
                    break

    cur = decl.get("Currency")
    rate = decl.get("Exchange Rate")
    if cur and rate and cur in CCY_BANDS:
        lo, hi = CCY_BANDS[cur]
        try:
            r = float(rate)
            if not (lo <= r <= hi):
                notes.append(f"currency band fail: {cur} rate {r} outside [{lo}-{hi}]")
        except Exception:
            pass

    cv = decl.get("Total Customs Value") or 0
    mf = decl.get("MACCS Service Fee (MF)")
    try:
        cvf = float(cv)
        if cvf > 0 and (mf in (None, 0, 0.0, "0", "")):
            decl["MACCS Service Fee (MF)"] = round(cvf * 0.02)
            notes.append(f"MACCS Fee determ: 2%×{int(cvf)}={round(cvf*0.02)}")
    except Exception:
        pass

    duty = decl.get("Import/Export Customs Duty") or 0
    ct = decl.get("Commercial Tax (CT)")
    try:
        if cv and (ct in (None, 0, 0.0)):
            decl["Commercial Tax (CT)"] = round((float(cv) + float(duty or 0)) * 0.05)
            notes.append("CT determ: 5%×(CV+Duty)")
    except Exception:
        pass

    cur2 = decl.get("Currency 2")
    if cur and cur2 and cur != cur2:
        notes.append(f"Currency 2 differs: {cur2} vs primary {cur} — verify")

    for it in items:
        if not it.get("Currency") and cur:
            it["Currency"] = cur
        if not it.get("Exchange Rate (1)") and rate:
            it["Exchange Rate (1)"] = rate
        hs = it.get("HS Code")
        if hs and isinstance(hs, str) and re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", hs.strip()):
            it["HS Code"] = hs.strip() + " 00"
    if any(re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\s00", it.get("HS Code") or "") for it in items):
        notes.append("HS code padded ' 00' suffix")

    d["_postfix_notes"] = notes
    return d


# ─── Orchestrator ──────────────────────────────────────────────────
def run_one(pdf_path: str) -> Dict:
    """
    Run the full V8 5-pass pipeline on a single PDF.

    Returns a flat dict suitable for FastAPI JSON response:
      {
        "declaration": {...},
        "items": [...],
        "document_format": "MACCS|CUSDEC1",
        "duration_seconds": float,
        "cost_usd": float,
        "cost_breakdown": [{label, model, in, out, cost}],
        "pages_kept": [int] or "ALL",
        "anomalies": [str],
        "postfix_notes": [str]
      }
    """
    global _COST_BUCKET
    _COST_BUCKET = []

    pdf = Path(pdf_path)
    t0 = time.time()
    print(f"\n{'='*70}\n[V8] DOC: {pdf}\n{'='*70}")

    # Optional cost-tracker integration (best-effort, no hard dep)
    cost_tracker = None
    try:
        import cost_tracker as _ct  # type: ignore
        cost_tracker = _ct
    except Exception:
        cost_tracker = None

    print("[Pass 0] Haiku page filter")
    try:
        sliced_pdf, kept = pass0_filter(pdf)
        print(f"  pages kept: {kept}")
        work_pdf = sliced_pdf
        pages_kept: object = kept
    except Exception as e:
        print(f"  Pass0 fail ({e}) — using full PDF")
        work_pdf = pdf
        pages_kept = "ALL"

    print("[Pass 1] Opus holistic")
    p1 = pass1(work_pdf)
    if not p1:
        return {
            "declaration": {},
            "items": [],
            "document_format": None,
            "duration_seconds": round(time.time() - t0, 1),
            "cost_usd": round(sum(c.get("cost") or 0 for c in _COST_BUCKET), 4),
            "cost_breakdown": list(_COST_BUCKET),
            "pages_kept": pages_kept,
            "anomalies": [],
            "postfix_notes": [],
            "error": "pass1_failed",
        }

    flagged = p1.get("uncertain_fields", [])
    print(f"  uncertain: {flagged}")

    if flagged:
        print("[Pass 2] Opus re-read flagged")
        p2 = pass2(work_pdf, p1)
        if p2:
            p1 = apply_corrections(p1, p2)
            print(f"  corrections applied: {list((p2.get('corrections') or {}).keys())}")
    else:
        print("[Pass 2] skipped — no uncertainty flags")

    print("[Pass 3] Sonnet independent")
    p3 = pass3(work_pdf)
    if not p3:
        print("  Pass3 fail — using Pass1+2 only")
        final_raw = p1
    else:
        print("[Pass 4] Opus reconciler")
        p4 = pass4(work_pdf, p1, p3)
        final_raw = p4 or p1

    flat = _flatten(final_raw)
    print("[Pass 5] Post-fix")
    flat = pass5(flat)
    for n in flat.get("_postfix_notes", []):
        print(f"  ▸ {n}")

    duration = round(time.time() - t0, 1)
    cost_total = round(sum(c.get("cost") or 0 for c in _COST_BUCKET), 4)

    # best-effort cost_tracker hook
    if cost_tracker is not None:
        for fn_name in ("track_cost", "add_cost", "record_cost"):
            fn = getattr(cost_tracker, fn_name, None)
            if callable(fn):
                try:
                    fn(cost_total, source="v8", pdf=str(pdf))
                except Exception:
                    pass
                break

    print(f"[V8] DONE in {duration}s  cost=${cost_total}")

    return {
        "declaration": flat.get("declaration", {}),
        "items": flat.get("items", []),
        "document_format": flat.get("document_format"),
        "duration_seconds": duration,
        "cost_usd": cost_total,
        "cost_breakdown": list(_COST_BUCKET),
        "pages_kept": pages_kept,
        "anomalies": flat.get("anomalies", []) or [],
        "postfix_notes": flat.get("_postfix_notes", []) or [],
    }
