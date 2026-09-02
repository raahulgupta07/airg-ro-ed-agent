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

# --- let route modules import without a database -----------------------------
# `from routes import evidence` pulls in `middleware` -> `auth`, and `auth`
# resolves its signing secret at import time: env var, else a value persisted in
# the `settings` table. On a dev box with no Postgres that second path raises and
# the import dies, so the pure helpers in a route module — `_flagged`, `_located`,
# `_row` — could not be tested at all without standing up a database.
#
# A fixed placeholder here is only ever seen by pytest in this process. It does
# NOT weaken the production path: nothing asserts on this value, no test covers
# secret resolution, and the real resolver is untouched. Set JWT_SECRET_KEY in
# the environment to override.
os.environ.setdefault(
    "JWT_SECRET_KEY",
    "pytest-only-not-a-real-secret-" + "0" * 32,
)

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
