"""V11 — Master router: dispatches typed pages to V7, HW pages to V10."""
import os

try:
    from config import API_KEY as OPENROUTER_API_KEY
except ImportError:
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

HAIKU  = "anthropic/claude-haiku-4-5"
SONNET = "anthropic/claude-sonnet-4-6"

CLASSIFIER_MODEL = HAIKU

# How aggressively to skip ATTACHMENT pages (drop entirely vs send to V7)
DROP_ATTACHMENTS = True

# ── V12 "Presto" — typed fast-path (Phase 0: rails only, OFF by default) ──
# When a TYPED page already has an extractable text layer (digital PDF), Presto
# can skip image render + per-page vision and structure the text in one call.
# Flag stays OFF until shadow-mode (Phase 4) proves accuracy ≥ V7.
PRESTO_ENABLED = os.getenv("PRESTO_ENABLED", "0").strip() not in ("0", "false", "False", "")
# Min chars of extractable text on a page to treat it as digital (has text layer).
PRESTO_TEXT_LAYER_MIN_CHARS = int(os.getenv("PRESTO_TEXT_LAYER_MIN_CHARS", "120"))
