"""Product lane — the one job is DON'T DROP A PRODUCT. The header pipeline only
routes the 1-2 declaration pages, so items on later pages of a multi-page customs
bundle (invoice / CUSDEC / packing list) are missed. This module finds EVERY item
page, extracts every goods row in one vision call, collapses the bundle repeats,
and reports the declared item count so the caller can flag an under-count."""
import re

from . import llm

# HS-code like 8471.30.10 — the strongest signal a page carries a goods table.
_HS_RE = re.compile(r"\d{4}\.\d{2}\.\d{2}")

# Case-insensitive text markers found on a goods/items table page.
_MARKERS = ("description of goods", "hscode", "14.hscode",
            "15.description")

_MAX_ITEM_PAGES = 6  # keep the multi-image request under OpenRouter's 30MB cap

_PROMPT = """You are reading the goods table(s) of a Myanmar Customs Declaration
bundle (CUSDEC / invoice / packing list). Extract EVERY product row you can see.

Return ONLY one JSON object:
{"items": [ {"no": <row no or null>, "hs_code": "...", "description": "...",
             "quantity": <number or null>, "unit": "...", "value": <number or null>,
             "unit_price": <number or null>, "customs_value": <number or null>} ]}

RULES (follow exactly):
- One object per product row. Do NOT merge rows and do NOT skip rows.
- Read quantities from the Quantity column (not the value/price column).
- On a CUSDEC / release-order item block ('No. 001  HS <code>', 'Item name',
  'Quantity (1)', 'Item value', 'Invoice unit price', 'Customs value'):
  value <- 'Item value', unit_price <- 'Invoice unit price',
  customs_value <- 'Customs value'.
  'Item value' and 'Invoice unit price' are in the INVOICE currency; 'Customs value'
  is the assessed MMK figure and is usually far larger. They are DIFFERENT UNITS —
  never copy one into the other, and never multiply by the exchange rate to invent
  customs_value: the assessed value may carry an uplift. No 'Customs value' printed
  on the row -> customs_value must be null.
- On an invoice / packing list / import-licence table there is no assessed value:
  set value from the amount column and leave customs_value null.
- Use null for any field that is absent on a row.
- If the same table repeats across pages, still list every row you see — the
  caller de-duplicates.
- Return ONLY the JSON object, no prose, no markdown fence."""


def _page_is_item_page(page) -> bool:
    """True if a page's text layer looks like a goods/items table."""
    txt = (page.text or "")
    low = txt.lower()
    if _HS_RE.search(txt):
        return True
    if any(m in low for m in _MARKERS):
        return True
    if "quantity" in low and "value" in low:
        return True
    return False


def find_item_pages(ctx) -> list:
    """Return the Page objects that carry product rows, in page order.

    A page qualifies on an HS-code pattern, a goods-table marker, or the
    Quantity+Value pair. Capped to the first _MAX_ITEM_PAGES so the multi-image
    request stays under OpenRouter's 30MB limit."""
    hits = [p for p in ctx.pages if _page_is_item_page(p)]
    hits.sort(key=lambda p: p.number)
    return hits[:_MAX_ITEM_PAGES]


def declared_item_count(ctx):
    """Scan the text layer for a 'Total items' label and return the integer
    beside it. Searches a small window of lines around the label for the first
    1-3 digit number. Returns None if not found."""
    lines = ctx.all_lines
    for i, line in enumerate(lines):
        if "total items" not in line.lower():
            continue
        # Look at the label line plus the next couple of lines for the count.
        for j in range(i, min(i + 3, len(lines))):
            m = re.search(r"\b(\d{1,3})\b", lines[j])
            if m:
                return int(m.group(1))
    return None


def extract_all_items(ctx, item_pages, model=None) -> dict:
    """Build image content from the given pages and make ONE vision call that
    returns every goods row. Fail-safe: any error -> empty items."""
    model = model or llm.PRIMARY
    try:
        image_content = [
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{p.image_b64}"}}
            for p in item_pages]
        parsed, raw, usage, err = llm.call(model, _PROMPT, image_content)
        if err:
            return {"items": [], "usage": usage or {}, "err": err}
        items = (parsed or {}).get("items") if isinstance(parsed, dict) else None
        return {"items": items if isinstance(items, list) else [],
                "usage": usage or {}, "err": None}
    except Exception as e:  # never raise into the pipeline
        return {"items": [], "usage": {}, "err": str(e)}


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def dedupe(items) -> list:
    """Collapse the bundle repeats — a customs bundle carries the same product
    across invoice / CUSDEC / packing list. Key by (hs_digits, round(value)); if
    value is missing, key by (hs_digits, description signature). Keep the FIRST
    occurrence of each key in order, then renumber `no` = 1..N."""
    seen = set()
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        hs = _digits(it.get("hs_code"))
        val = _to_float(it.get("value"))
        if val is not None:
            key = (hs, round(val))
        else:
            desc = re.sub(r"\s+", "", str(it.get("description") or "").upper())[:30]
            key = (hs, desc)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    for n, it in enumerate(out, start=1):
        it["no"] = n
    return out


def run(ctx, model=None) -> dict:
    """Orchestrate the product lane: find item pages, extract every row in one
    call, de-duplicate the bundle repeats, and report the declared count so the
    caller can flag an under-count."""
    all_hits = [p for p in ctx.pages if _page_is_item_page(p)]
    pages = find_item_pages(ctx)
    res = extract_all_items(ctx, pages, model)
    items = dedupe(res["items"])
    declared = declared_item_count(ctx)
    return {"items": items,
            "n_items": len(items),
            "declared_count": declared,
            "item_pages": [p.number for p in pages],
            "capped": len(all_hits) > _MAX_ITEM_PAGES,
            "usage": res.get("usage", {}),
            "err": res.get("err")}
