"""Golden triage tests — pin the digital-vs-scanned CUSDEC decision per doc.

The keystone invariant: the CUSDEC page's `cusdec_page_digital` flag is what routes
a job to deterministic text rescue vs vision rescue. If detection drifts, scanned
docs silently skip rescue again (exactly the class of bug this suite guards). These
run only when the PDF corpus is present (skip cleanly otherwise).
"""
import os
import glob
import pytest

fitz = pytest.importorskip("fitz")

from v11.triage import compute_triage  # noqa: E402
from v11.agents.page_classifier import _probe_text_layers, _attach_text_layer  # noqa: E402

PDF_DIR = os.environ.get(
    "RO_ED_TEST_PDFS",
    "/private/tmp/claude-501/-Users-rahulgupta/7e2d3e61-b924-439a-8907-69d9d052f043/scratchpad/pdfs",
)

# Docs whose CUSDEC page carries a real text layer → deterministic rescue.
DIGITAL_CUSDEC = {
    "100313488550", "100313868761", "100313870641", "100314743761", "100319699762",
}
# Docs whose CUSDEC page is a scan (image) → must route to vision rescue.
SCANNED_CUSDEC = {
    "100304950542", "100305819941", "100305869051", "100306351722", "100306920231",
    "100306920561", "100306920821", "100306922661", "100308480420",
    "MA0259100405", "MA0259100560",
}


def _triage_for(path):
    doc = fitz.open(path)
    npg = doc.page_count
    doc.close()
    probe = _probe_text_layers(path)
    pages = [{"page": i, "label": "TYPED"} for i in range(1, npg + 1)]
    _attach_text_layer(pages, probe)
    return compute_triage(path, {"pages": pages, "n_pages": npg})


def _corpus():
    if not os.path.isdir(PDF_DIR):
        return []
    return sorted(glob.glob(os.path.join(PDF_DIR, "*.pdf")))


@pytest.mark.parametrize("stem", sorted(DIGITAL_CUSDEC))
def test_digital_cusdec_routes_deterministic(stem):
    path = os.path.join(PDF_DIR, stem + ".pdf")
    if not os.path.exists(path):
        pytest.skip(f"corpus PDF absent: {stem}")
    t = _triage_for(path)
    assert t["cusdec_page_digital"] is True, f"{stem}: CUSDEC should be text-digital"
    assert t["needs_vision_rescue"] is False, f"{stem}: should use deterministic rescue"


@pytest.mark.parametrize("stem", sorted(SCANNED_CUSDEC))
def test_scanned_cusdec_routes_vision(stem):
    path = os.path.join(PDF_DIR, stem + ".pdf")
    if not os.path.exists(path):
        pytest.skip(f"corpus PDF absent: {stem}")
    t = _triage_for(path)
    assert t["cusdec_page_digital"] is False, f"{stem}: CUSDEC is a scan, not text"
    assert t["needs_vision_rescue"] is True, f"{stem}: must route to vision rescue"


def test_triage_never_raises_on_garbage():
    # Missing path / empty cls → safe review-biased default, no exception.
    t = compute_triage("/nonexistent/none.pdf", {"pages": [], "n_pages": 0})
    assert t["needs_vision_rescue"] is True
    assert t["doc_class"] in ("DIGITAL", "SCANNED", "MIXED")
