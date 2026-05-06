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
