"""Zoom-read tool — recover ONE field by rendering a HIGH-DPI crop of just the
region around it and asking the vision model to read only that field.

Targets the two open gaps the fleet still misses: faint/scrambled quantities and
low-res exchange rates on scanned pages. A tightly-cropped 400-DPI image reads
small print far better than the whole-page 150-DPI image the fleet loads by default.
OpenRouter-only (via `llm.call`), single-field, fail-safe."""
import base64

import fitz  # PyMuPDF

from . import llm


def find_field_region(ctx, label):
    """Locate `label`'s text on the page and return the region likely to hold its
    value. Returns (page_number, (x0,y0,x1,y1)) where the rect is a FRACTIONAL bbox
    (0..1 of page width/height), expanded right/down to swallow the value beside or
    below the label. Returns None if the label isn't in any text layer (scanned page)."""
    doc = fitz.open(ctx.pdf_path)
    try:
        for i in range(len(doc)):
            page = doc[i]
            hits = page.search_for(label)
            if not hits:
                continue
            r = hits[0]                      # first occurrence wins
            pr = page.rect
            pw, ph = pr.width, pr.height
            if pw <= 0 or ph <= 0:
                return None
            # Expand: small margin left/up, wide right (+45% w) + a little down (+3% h)
            # so the value sitting to the right of (or just under) the label is included.
            x0 = r.x0 - 0.02 * pw
            y0 = r.y0 - 0.01 * ph
            x1 = r.x1 + 0.45 * pw
            y1 = r.y1 + 0.03 * ph
            # To fractions of the page, clamped to [0, 1].
            fx0 = min(max(x0 / pw, 0.0), 1.0)
            fy0 = min(max(y0 / ph, 0.0), 1.0)
            fx1 = min(max(x1 / pw, 0.0), 1.0)
            fy1 = min(max(y1 / ph, 0.0), 1.0)
            return (i + 1, (fx0, fy0, fx1, fy1))
        return None
    finally:
        doc.close()


def zoom_read(ctx, page_number, region, field_name, model=None, dpi=400):
    """Render just `region` (fractional bbox) of page `page_number` at `dpi`, JPEG-
    encode it, and ask the model to read ONLY `field_name`. Returns
    {"value", "confidence", "usage", "err"}. Fail-safe: any exception ->
    value None / confidence 0 / err set (never raises)."""
    model = model or llm.PRIMARY
    doc = None
    try:
        doc = fitz.open(ctx.pdf_path)
        page = doc[page_number - 1]
        pr = page.rect
        fx0, fy0, fx1, fy1 = region
        clip = fitz.Rect(pr.x0 + fx0 * pr.width,
                         pr.y0 + fy0 * pr.height,
                         pr.x0 + fx1 * pr.width,
                         pr.y0 + fy1 * pr.height)
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), clip=clip)
        jpg = pix.tobytes("jpg", jpg_quality=85)
        b64 = base64.b64encode(jpg).decode("utf-8")
        image_content = [{"type": "image_url",
                          "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]
        prompt = (
            f"This is a zoomed crop of a Myanmar customs form. Read ONLY the "
            f"{field_name}. Return JSON {{\"value\": <value or null>, "
            f"\"confidence\": <0..1>}}. Return ONLY JSON."
        )
        parsed, raw, usage, err = llm.call(model, prompt, image_content, max_tokens=300)
        if parsed is None:
            return {"value": None, "confidence": 0,
                    "usage": usage or {}, "err": err or "no json"}
        return {"value": parsed.get("value"),
                "confidence": parsed.get("confidence", 0),
                "usage": usage or {},
                "err": err}
    except Exception as e:  # noqa: BLE001 — fail-safe by contract
        return {"value": None, "confidence": 0, "usage": {}, "err": str(e)}
    finally:
        if doc is not None:
            doc.close()


def zoom_field(ctx, label, field_name, model=None):
    """Convenience: find the region around `label` and zoom-read `field_name` from it.
    Returns {"value", "confidence", "err"} (region not found -> value None) or the
    full zoom_read dict on success."""
    region = find_field_region(ctx, label)
    if region is None:
        return {"value": None, "confidence": 0, "err": "region not found"}
    page, box = region
    return zoom_read(ctx, page, box, field_name, model)
