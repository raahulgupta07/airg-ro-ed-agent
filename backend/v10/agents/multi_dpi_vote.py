"""V10 — Multi-DPI multi-model vote for critical handwritten fields.

For a given field, render the page (or hint region) at 200/400/600 DPI.
Send all 3 renders + the field name + current value to claude-opus-4.7,
gemini-pro-latest, and claude-sonnet-4-6 in parallel.

Take majority vote on the value. If 3 disagree → flag for human review.
"""
import base64, io, json, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz
import requests
from PIL import Image

from v10.config import OPUS, GEMINI_PRO, SONNET, OPENROUTER_API_KEY


API_URL = "https://openrouter.ai/api/v1/chat/completions"


def _render_page(pdf_path: str, page: int, dpi: int) -> Optional[str]:
    try:
        doc = fitz.open(pdf_path)
        if page < 1 or page > len(doc):
            doc.close(); return None
        pix = doc[page - 1].get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        max_dim = {200: 1600, 400: 2400, 600: 3000, 800: 3200}.get(dpi, 2400)
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92, optimize=True)
        doc.close()
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"[MultiVote] render error dpi={dpi}: {e}")
        return None


def _build_prompt(field_name: str, current_value: str, hint: str) -> str:
    return f"""You are reading a Myanmar customs document at multiple zoom levels.
The same page is shown at 200/400/600 DPI. Look at all three to read precisely.

Target field: {field_name}
Current draft value: {current_value!r}
Location hint: {hint or "scan the form for this field's labeled box"}

CRITICAL HANDWRITING RULES:
- Read each digit/letter literally — exact pen strokes.
- Confusable pairs: 0↔8, 1↔7, 3↔8, 5↔6, 9↔0. Pick what you SEE, not what you expect.
- Decimal point = actual dot stroke. "60.8090" stays 60.8090, do NOT collapse.
- If unsure between 2 candidates, output "UNCERTAIN_<a>_OR_<b>" not a guess.

Output strict JSON only:
{{"value": "...", "confidence": "high|med|low", "evidence": "what you see", "alt_candidates": ["..."]}}"""


def _ask_one(model: str, parts: List[Dict], label: str) -> Optional[Dict]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": parts}],
        "temperature": 0,
        "max_tokens": 800,
    }
    for attempt in range(3):
        try:
            t0 = time.time()
            r = requests.post(API_URL,
                              headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                                       "Content-Type": "application/json"},
                              json=payload, timeout=180)
            dt = time.time() - t0
            print(f"[MultiVote-{label}] HTTP {r.status_code} {dt:.1f}s ({model.split('/')[-1]})")
            if r.status_code == 200:
                raw = r.json()["choices"][0]["message"]["content"]
                s = raw.strip()
                if s.startswith("```"):
                    s = s.split("```", 2)[1]
                    if s.startswith("json"): s = s[4:]
                if "{" in s:
                    s = s[s.index("{"):s.rindex("}") + 1]
                    s = re.sub(r",(\s*[\}\]])", r"\1", s)
                    try:
                        return json.loads(s)
                    except Exception:
                        pass
                return None
            elif r.status_code == 429:
                time.sleep(2 ** (attempt + 1)); continue
            else:
                print(f"[MultiVote-{label}] body: {r.text[:200]}")
        except Exception as e:
            print(f"[MultiVote-{label}] exc: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return None


def vote_field(pdf_path: str, page: int, field_name: str,
               current_value: str = "", hint: str = "",
               dpis: Tuple[int, ...] = (200, 400, 600)) -> Dict:
    """Multi-DPI multi-model vote on one field.

    Returns:
      {
        "winner": str | None,
        "winner_count": int,
        "votes": [{"model": str, "value": str, "confidence": str, "evidence": str}],
        "consensus": "unanimous|majority|tie",
        "needs_review": bool,
      }
    """
    # Render all DPI levels
    renders = []
    for dpi in dpis:
        b64 = _render_page(pdf_path, page, dpi)
        if b64:
            renders.append((dpi, b64))
    if not renders:
        return {"winner": current_value, "winner_count": 0, "votes": [],
                "consensus": "no_renders", "needs_review": True}

    # Build content parts: each render labeled
    parts = []
    for dpi, b64 in renders:
        parts.append({"type": "text", "text": f"--- Page {page} at {dpi} DPI ---"})
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    parts.append({"type": "text", "text": _build_prompt(field_name, current_value, hint)})

    # Three models in parallel
    models = [(OPUS, "opus"), (GEMINI_PRO, "gemini-pro"), (SONNET, "sonnet")]
    results = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(_ask_one, m, parts, label): label for m, label in models}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                res = fut.result()
                if res:
                    results.append({"model": label,
                                    "value": str(res.get("value", "")).strip(),
                                    "confidence": res.get("confidence", "low"),
                                    "evidence": res.get("evidence", "")[:120]})
            except Exception as e:
                print(f"[MultiVote-{label}] thread exc: {e}")

    if not results:
        return {"winner": current_value, "winner_count": 0, "votes": [],
                "consensus": "no_results", "needs_review": True}

    # Tally by normalized string
    from collections import Counter
    norm = lambda s: re.sub(r"\s+", "", str(s)).upper().strip()
    tally = Counter(norm(r["value"]) for r in results if r["value"])
    if not tally:
        return {"winner": current_value, "winner_count": 0, "votes": results,
                "consensus": "no_value", "needs_review": True}
    winner_norm, count = tally.most_common(1)[0]
    # Pick the actual original-case value matching winner_norm
    winner = next((r["value"] for r in results if norm(r["value"]) == winner_norm), current_value)
    consensus = ("unanimous" if count == len(results)
                 else "majority" if count > len(results) / 2
                 else "tie")
    return {
        "winner": winner,
        "winner_count": count,
        "votes": results,
        "consensus": consensus,
        "needs_review": consensus == "tie",
    }
