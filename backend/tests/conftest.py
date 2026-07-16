"""Pytest fixtures + path setup for the golden regression suite.

Runs from the repo root or from `backend/`. Ensures `backend/` is on
`sys.path` so `from v11.tools import ...` resolves the same way the worker
imports it (repo on sys.path), and locates the external PDF corpus.
"""
import json
import os
import sys

import pytest

# --- make `from v11.tools import ...` importable regardless of pytest CWD ----
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

# --- corpus location (PDFs live OUTSIDE the repo — see tests/README.md) ------
_DEFAULT_PDF_DIR = (
    "/private/tmp/claude-501/-Users-rahulgupta/"
    "7e2d3e61-b924-439a-8907-69d9d052f043/scratchpad/pdfs"
)


def pdf_dir() -> str:
    """Resolved PDF corpus directory (env override → scratchpad fallback)."""
    return os.environ.get("RO_ED_TEST_PDFS", _DEFAULT_PDF_DIR)


@pytest.fixture(scope="session")
def PDF_DIR() -> str:
    """Directory holding the real CUSDEC PDF fixtures."""
    return pdf_dir()


@pytest.fixture(scope="session")
def golden() -> dict:
    """Version-controlled ground-truth (copied next to the tests)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "golden_truth.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
