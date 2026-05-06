"""V9 — PDF manipulation tools."""
import base64, io, os
from pathlib import Path
from typing import List

import fitz
from PIL import Image
from agno.tools import tool


@tool(show_result=False)
def render_page(pdf_path: str, page: int, dpi: int = 200) -> str:
    """Render a single PDF page as a base64-encoded JPEG.

    Use this to inspect a page visually. Page index is 1-based.
    Higher dpi (300-400) for tight reads. Default 200 is balanced.

    Returns base64 JPEG string."""
    doc = fitz.open(pdf_path)
    if page < 1 or page > len(doc):
        raise ValueError(f"page {page} out of range (1-{len(doc)})")
    pix = doc[page - 1].get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    # downsample if huge
    max_dim = 2400
    if img.width > max_dim or img.height > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    doc.close()
    return base64.b64encode(buf.getvalue()).decode()


@tool(show_result=False)
def extract_text(pdf_path: str, page: int = 0) -> str:
    """Extract embedded text from a PDF page (no OCR).

    Use this to find which pages contain declaration markers like
    'Declaration No', 'Release Order', 'CUSDEC', 'Box 11'.
    Returns empty string for pure-image pages.

    page=0 returns concatenated text of ALL pages (with page markers).
    page>=1 returns just that page's text."""
    doc = fitz.open(pdf_path)
    if page == 0:
        out = []
        for i, p in enumerate(doc, 1):
            out.append(f"--- Page {i} ---\n{p.get_text() or ''}")
        text = "\n".join(out)
    else:
        if page < 1 or page > len(doc):
            doc.close()
            raise ValueError(f"page {page} out of range")
        text = doc[page - 1].get_text() or ""
    doc.close()
    return text


@tool(show_result=False)
def slice_pdf(pdf_path: str, pages: List[int]) -> str:
    """Create a new PDF containing only the specified pages.

    Use this after page filtering to reduce input size.
    pages is a list of 1-based indices to keep.

    Returns the file path of the new sliced PDF."""
    if not pages:
        raise ValueError("pages list cannot be empty")
    src = fitz.open(pdf_path)
    dst = fitz.open()
    for p in sorted(set(pages)):
        if 1 <= p <= len(src):
            dst.insert_pdf(src, from_page=p - 1, to_page=p - 1)
    out_dir = os.environ.get("V9_TMP_DIR", "/tmp/v9_results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"_sliced_{Path(pdf_path).stem}.pdf")
    dst.save(out_path)
    dst.close()
    src.close()
    return out_path


@tool(show_result=False)
def box_zoom(pdf_path: str, page: int, x: float, y: float,
             w: float, h: float, dpi: int = 400) -> str:
    """Crop a rectangular region of a page at high DPI.

    Use this for blurry digit/letter reads — render only the suspect
    box (e.g. Box 11 declaration number) at 400 DPI for precision.

    Coordinates x, y, w, h are in PDF points (1/72 inch).
    A typical CUSDEC1 box is ~150×30 pt.

    Returns base64 JPEG of the cropped region."""
    doc = fitz.open(pdf_path)
    if page < 1 or page > len(doc):
        doc.close()
        raise ValueError(f"page {page} out of range")
    rect = fitz.Rect(x, y, x + w, y + h)
    pix = doc[page - 1].get_pixmap(dpi=dpi, clip=rect)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92, optimize=True)
    doc.close()
    return base64.b64encode(buf.getvalue()).decode()
