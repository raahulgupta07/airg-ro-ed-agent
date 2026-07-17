"""L1 — page router. Every scored field on a CUSDEC lives on the Release-Order
notification page(s); the rest are B/L scans, packing lists, permits. Send only
the pages that carry fields → ~70% fewer image tokens, same information."""
from typing import List

from .context import DocContext, Page

# Signals that a page holds the header / value / tax fields we extract.
_KEYWORDS = (
    "release order", "total customs value", "taxes and fees", "exchange rate",
    "invoice price", "declaration no", "customs duty", "commercial tax",
    "maccs", "importer",
)


def _score(page: Page) -> int:
    low = page.text.lower()
    return sum(1 for k in _KEYWORDS if k in low)


def select(ctx: DocContext, max_pages: int = 2) -> List[Page]:
    """Pick the field-bearing pages. Digital pages score by keyword; if the doc is
    scanned (no text layer) we cannot score, so fall back to the first image pages
    (the RO notification is always at the front)."""
    scored = [(p, _score(p)) for p in ctx.pages]
    hits = [p for p, s in scored if s >= 2]
    if hits:
        # keep the strongest, in page order, capped
        hits = sorted(hits, key=lambda p: -_score(p))[:max_pages]
        return sorted(hits, key=lambda p: p.number)
    # scanned doc → front image pages
    imgs = [p for p in ctx.pages if p.is_image_page][:max_pages]
    return imgs or ctx.pages[:max_pages]


def image_content(pages: List[Page]) -> list:
    return [{"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{p.image_b64}"}}
            for p in pages]
