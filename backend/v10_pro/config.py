"""V9 — Agno-based agentic pipeline configuration.
Reuses V7 model set via OpenRouter."""

import os

# OpenRouter API key (reuse V7 config)
try:
    from config import API_KEY as OPENROUTER_API_KEY
except ImportError:
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# V7 model set
OPUS    = "anthropic/claude-opus-4.7"
SONNET  = "anthropic/claude-sonnet-4-6"
HAIKU   = "anthropic/claude-haiku-4-5"
GEMINI_FLASH = "google/gemini-3-flash-preview"
GEMINI_PRO   = "~google/gemini-pro-latest"

# Per-role assignments
MASTER_MODEL    = OPUS         # orchestrator with tools
READER_MODEL    = GEMINI_PRO   # holistic PDF reader (V7 solo path)
VERIFIER_MODEL  = SONNET       # cross-check
FILTER_MODEL    = HAIKU        # page classifier (DECL vs ATTACHMENT)
ASSEMBLER_MODEL = GEMINI_FLASH # cheap fallback

# Currency rate plausibility bands (foreign → MMK)
CCY_BANDS = {
    "THB": (40, 110),  "USD": (1300, 5500), "EUR": (1500, 6500),
    "KRW": (1.0, 5.0), "JPY": (10, 50),     "CNY": (200, 900),
    "SGD": (1500, 4500), "GBP": (2000, 7000),
}
