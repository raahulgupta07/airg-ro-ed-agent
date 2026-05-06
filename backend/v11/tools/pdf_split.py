"""V11 — PDF splitter by page labels.
Given a PDF and a labels-per-page list, write separate PDFs for each label."""
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz


def split_pdf_by_labels(pdf_path: str, page_labels: List[Dict]) -> Dict[str, Optional[str]]:
    """Split a PDF into per-label slices.

    Args:
        pdf_path: input PDF path
        page_labels: list of {"page": int, "label": str} (1-based page indices)

    Returns:
        {
          "TYPED": "/tmp/.../typed_slice.pdf"   | None if no typed pages
          "HANDWRITTEN": "/tmp/.../hw_slice.pdf" | None
          "ATTACHMENT": "/tmp/.../attach_slice.pdf" | None
          "page_buckets": {"TYPED": [1,3], "HANDWRITTEN": [2], "ATTACHMENT": [4,5]},
        }
    """
    out_dir = Path(os.environ.get("V11_TMP_DIR", "/tmp/v11_results"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(pdf_path).stem

    buckets: Dict[str, List[int]] = {"TYPED": [], "HANDWRITTEN": [], "ATTACHMENT": []}
    for entry in page_labels:
        label = (entry.get("label") or "TYPED").upper()
        page = entry.get("page")
        if not isinstance(page, int):
            continue
        if label not in buckets:
            buckets["TYPED"].append(page)
        else:
            buckets[label].append(page)

    src = fitz.open(str(pdf_path))
    n = len(src)
    out_paths: Dict[str, Optional[str]] = {"TYPED": None, "HANDWRITTEN": None, "ATTACHMENT": None}

    for label, pages in buckets.items():
        valid = sorted({p for p in pages if 1 <= p <= n})
        if not valid:
            continue
        dst = fitz.open()
        for p in valid:
            dst.insert_pdf(src, from_page=p - 1, to_page=p - 1)
        out = out_dir / f"v11_{label.lower()}_{stem}.pdf"
        dst.save(str(out))
        dst.close()
        out_paths[label] = str(out)
    src.close()
    return {**out_paths, "page_buckets": buckets}
