"""D — handwriting boost. Scanned/handwritten CUSDECs (MA0259…) are the accuracy
frontier: the embedded scan the native-PDF reader sees is often low-res, so digits
and item rows get missed. When a doc is mostly image-pages AND the base extraction
came back weak (missing items or an empty value block), re-read the pages as
HIGH-DPI images with a handwriting-focused prompt and merge in what was missing.

Bounded (one extra call, capped pages), flag-gated (ROVER_HANDWRITING_BOOST), and
fail-safe (any error -> return nothing, base result untouched). Never overwrites a
value the base read already has — only FILLS empties and ADDS unseen items."""
import base64
import os

import fitz

from . import llm
from .schema import Cell
from .single_agent import _PROMPT, _COLS

HW_BOOST = os.environ.get("ROVER_HANDWRITING_BOOST") == "1"
HW_DPI = int(os.environ.get("ROVER_HW_DPI", "300"))
HW_MAX_PAGES = int(os.environ.get("ROVER_HW_MAX_PAGES", "8"))

_HINT = ("\n\nIMPORTANT: this is a SCANNED / HANDWRITTEN customs document. Read "
         "handwritten and faint printed digits with extra care — distinguish 1/7, "
         "0/6/8, 3/5/8, 2/7. Prefer null over a guess when a digit is unreadable. "
         "Capture EVERY product line, including handwritten continuation rows.")


def is_handwritten(ctx) -> bool:
    """A doc dominated by image (no-text) pages — where a text-layer read is useless."""
    if not ctx.pages:
        return False
    img = sum(1 for p in ctx.pages if p.is_image_page)
    return img >= max(1, (len(ctx.pages) + 1) // 2)


def _hires_images(pdf_path: str) -> list:
    doc = fitz.open(pdf_path)
    z = HW_DPI / 72.0
    out = []
    try:
        for i in range(min(HW_MAX_PAGES, len(doc))):
            pix = doc[i].get_pixmap(matrix=fitz.Matrix(z, z))
            jpg = pix.tobytes("jpg", jpg_quality=85)
            out.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(jpg).decode()}"}})
    finally:
        doc.close()
    return out


def boost(pdf_path: str, model: str = None):
    """Hi-res image re-read with the handwriting hint. Returns
    {cells: {col: Cell}, items: [...], usage: {}} — empty on any failure."""
    model = model or llm.PRIMARY
    try:
        imgs = _hires_images(pdf_path)
        parsed, raw, usage, err = llm.call(model, _PROMPT + _HINT, imgs, max_tokens=8000)
    except Exception:
        return {"cells": {}, "items": [], "usage": {}}
    if not isinstance(parsed, dict):
        return {"cells": {}, "items": [], "usage": usage or {}}
    cells = {}
    for col in _COLS:
        obj = parsed.get(col)
        if isinstance(obj, dict):
            c = Cell(column=col, model=f"{model}(hw)")
            c.value = obj.get("value")
            c.confidence = float(obj.get("confidence") or 0)
            c.status = "ok" if c.value not in (None, "") else "empty"
            cells[col] = c
    items = parsed.get("items")
    return {"cells": cells, "items": items if isinstance(items, list) else [], "usage": usage or {}}
