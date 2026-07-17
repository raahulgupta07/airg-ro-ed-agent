"""Doc context: rendered page images (for vision) + text layer (for deterministic).
Shared, read-only blackboard the column-agents draw from."""
import base64
from dataclasses import dataclass, field
from typing import List, Dict

import fitz  # PyMuPDF

DPI = 150  # JPEG at 150 keeps the whole doc under OpenRouter's 30MB/request cap


@dataclass
class Page:
    number: int
    text: str
    lines: List[str]
    image_b64: str          # JPEG base64 (no data: prefix)
    is_image_page: bool     # <40 chars of text -> scanned page


@dataclass
class DocContext:
    pdf_path: str
    pages: List[Page] = field(default_factory=list)

    @property
    def all_lines(self) -> List[str]:
        out = []
        for p in self.pages:
            out.extend(p.lines)
        return out

    def image_content(self) -> List[Dict]:
        """OpenRouter multimodal content blocks for every page image."""
        return [{"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{p.image_b64}"}}
                for p in self.pages]

    def full_text(self) -> str:
        return "\n".join(f"[page {p.number}]\n{p.text}" for p in self.pages)


def load(pdf_path: str) -> DocContext:
    doc = fitz.open(pdf_path)
    z = DPI / 72.0
    ctx = DocContext(pdf_path=pdf_path)
    for i in range(len(doc)):
        page = doc[i]
        txt = page.get_text() or ""
        lines = [l.strip() for l in txt.splitlines() if l.strip()]
        pix = page.get_pixmap(matrix=fitz.Matrix(z, z))
        jpg = pix.tobytes("jpg", jpg_quality=75)
        ctx.pages.append(Page(
            number=i + 1,
            text=txt,
            lines=lines,
            image_b64=base64.b64encode(jpg).decode("utf-8"),
            is_image_page=len(txt.strip()) < 40,
        ))
    doc.close()
    return ctx
