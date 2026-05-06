"""V10 — Handwriting detector.
Two-stage check: text-extract heuristic, then optional Haiku visual verify.

Returns:
    {"is_hw": bool, "confidence": float, "reason": str,
     "text_chars": int, "marker_count": int, "method": "text|visual|both"}
"""
import base64, io
from pathlib import Path
from typing import Dict
import fitz
from PIL import Image


TYPED_MARKERS = (
    "DECLARATION NO", "RELEASE ORDER", "TOTAL CUSTOMS VALUE",
    "BOX 11", "TOTAL ITEMS", "MACCS", "CUSDEC",
    "EXEMPTION/REDUCTION", "MMK", "FOREIGN CURRENCY",
)


def text_check(pdf_path: str, n_pages: int = 3) -> Dict:
    """Pure-Python text-extract heuristic. Returns is_hw + reason."""
    try:
        doc = fitz.open(str(pdf_path))
        if len(doc) == 0:
            doc.close()
            return {"is_hw": True, "confidence": 1.0,
                    "reason": "empty PDF", "text_chars": 0,
                    "marker_count": 0, "method": "text"}
        text = ""
        for i in range(min(n_pages, len(doc))):
            text += doc[i].get_text() or ""
        total = len(doc)
        doc.close()
    except Exception as e:
        return {"is_hw": True, "confidence": 0.5,
                "reason": f"read error: {e}", "text_chars": 0,
                "marker_count": 0, "method": "text"}

    text_upper = text.upper()
    markers = sum(1 for m in TYPED_MARKERS if m in text_upper)
    chars = len(text)

    # Strong typed signal
    if markers >= 2 and chars >= 500:
        return {"is_hw": False, "confidence": 0.95,
                "reason": f"strong typed: {markers} markers, {chars} chars",
                "text_chars": chars, "marker_count": markers, "method": "text"}
    # Strong handwritten signal
    if chars < 50 and markers == 0:
        return {"is_hw": True, "confidence": 0.95,
                "reason": f"image-only ({chars} chars, no markers)",
                "text_chars": chars, "marker_count": markers, "method": "text"}
    # Moderate
    if markers >= 1 and chars >= 300:
        return {"is_hw": False, "confidence": 0.75,
                "reason": f"typed ({markers} markers, {chars} chars)",
                "text_chars": chars, "marker_count": markers, "method": "text"}
    # Borderline — needs visual
    return {"is_hw": True, "confidence": 0.55,
            "reason": f"borderline: {markers} markers, {chars} chars — visual check recommended",
            "text_chars": chars, "marker_count": markers, "method": "text"}


def visual_check(pdf_path: str) -> Dict:
    """Render page 1, ask Haiku: handwritten or typed? Returns dict."""
    import json, re, time, requests
    from v10.config import HAIKU, OPENROUTER_API_KEY

    try:
        doc = fitz.open(str(pdf_path))
        if len(doc) == 0:
            doc.close()
            return {"is_hw": True, "confidence": 0.5, "reason": "empty",
                    "text_chars": 0, "marker_count": 0, "method": "visual"}
        pix = doc[0].get_pixmap(dpi=80)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        max_dim = 1000
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        doc.close()
    except Exception as e:
        return {"is_hw": True, "confidence": 0.5, "reason": f"render error: {e}",
                "text_chars": 0, "marker_count": 0, "method": "visual"}

    payload = {
        "model": HAIKU,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text",
             "text": ("Is this Myanmar customs document HANDWRITTEN (CUSDEC-1 form, ink pen filled) "
                      "or TYPED (MACCS Release Order, machine-printed)?\n"
                      "Output strict JSON: {\"format\":\"HANDWRITTEN|TYPED\",\"confidence\":\"high|med|low\",\"reason\":\"...\"}")},
        ]}],
        "temperature": 0,
        "max_tokens": 200,
    }
    for attempt in range(2):
        try:
            r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                              headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                                       "Content-Type": "application/json"},
                              json=payload, timeout=60)
            if r.status_code == 200:
                raw = r.json()["choices"][0]["message"]["content"]
                s = raw.strip()
                if s.startswith("```"):
                    s = s.split("```", 2)[1]
                    if s.startswith("json"): s = s[4:]
                if "{" in s:
                    s = s[s.index("{"):s.rindex("}") + 1]
                    s = re.sub(r",(\s*[\}\]])", r"\1", s)
                    parsed = json.loads(s)
                    fmt = (parsed.get("format") or "").upper()
                    is_hw = fmt == "HANDWRITTEN"
                    conf_str = (parsed.get("confidence") or "").lower()
                    conf_map = {"high": 0.95, "med": 0.75, "low": 0.55}
                    return {"is_hw": is_hw,
                            "confidence": conf_map.get(conf_str, 0.7),
                            "reason": parsed.get("reason", "")[:120],
                            "text_chars": 0, "marker_count": 0, "method": "visual"}
        except Exception as e:
            if attempt == 1:
                return {"is_hw": True, "confidence": 0.5, "reason": f"haiku error: {e}",
                        "text_chars": 0, "marker_count": 0, "method": "visual"}
            time.sleep(2)
    return {"is_hw": True, "confidence": 0.5, "reason": "haiku no response",
            "text_chars": 0, "marker_count": 0, "method": "visual"}


def detect(pdf_path: str, use_visual: bool = True) -> Dict:
    """Combined detection. Run text check first; if borderline (conf<0.7), run visual."""
    t = text_check(pdf_path)
    if t["confidence"] >= 0.85 or not use_visual:
        return t
    v = visual_check(pdf_path)
    # Combine — visual wins on low-conf text
    return {
        "is_hw": v["is_hw"],
        "confidence": (t["confidence"] + v["confidence"]) / 2,
        "reason": f"text:{t['reason']} | visual:{v['reason']}",
        "text_chars": t["text_chars"],
        "marker_count": t["marker_count"],
        "method": "both",
    }
