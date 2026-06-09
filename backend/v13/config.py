"""V13 "Scribe" — handwriting / scanned-ink extraction engine (standalone).

Separate from V10 PRO; changes no existing pipeline. OFF by default — wired into
nothing until explicitly enabled. Handwritten/scanned pages have NO text layer,
so Scribe is image-based: high-DPI render → strong VLM with a strict schema →
self-consistency voting on critical numbers → arithmetic gates → flag uncertain.
"""
import os

try:
    from config import API_KEY as OPENROUTER_API_KEY
except ImportError:
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# Vision model for handwriting. gemini-3-flash returns COMPLETE JSON (gemini-2.5-pro
# is a reasoning model that burns the token budget on thinking and truncates
# mid-JSON on dense forms → empty results). Flash + N-way voting = accurate,
# complete, fast, cheap. Override via env.
SCRIBE_MODEL = os.getenv("SCRIBE_MODEL", "google/gemini-3-flash-preview")

# Render DPI for ink (higher than typed — handwriting needs detail).
SCRIBE_DPI = int(os.getenv("SCRIBE_DPI", "300"))

# Self-consistency: how many independent reads to vote across (odd number).
SCRIBE_VOTES = int(os.getenv("SCRIBE_VOTES", "3"))

# Cross-MODEL agreement: rotate votes across these models so field_confidence
# reflects agreement between INDEPENDENT models (different vendors), not just
# temperature noise on one model. Fields two models agree on are trustworthy;
# disagreements get flagged for review.
SCRIBE_MODELS = [m.strip() for m in os.getenv(
    "SCRIBE_MODELS",
    "google/gemini-3-flash-preview,anthropic/claude-haiku-4-5").split(",") if m.strip()]

# Temperature for the voting reads (>0 so samples differ; low so they stay sane).
SCRIBE_TEMPERATURE = float(os.getenv("SCRIBE_TEMPERATURE", "0.4"))

# Master on/off. Nothing routes to Scribe unless this is set.
SCRIBE_ENABLED = os.getenv("SCRIBE_ENABLED", "0").strip() not in ("0", "false", "False", "")
