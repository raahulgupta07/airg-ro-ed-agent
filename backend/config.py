#!/usr/bin/env python3
"""
Configuration file for PDF extraction pipeline
Centralized settings for all steps
"""

import os
import time
from pathlib import Path

# ============================================================================
# BASE DIRECTORY
# ============================================================================

BASE_DIR = Path(__file__).parent

# ============================================================================
# APP VERSION (single source of truth — shown in UI footer + /api/health)
# CalVer year.month.patch; bump patch on each shipped change so a deploy is
# verifiable at a glance ("is AWS running the latest?").
# ============================================================================
APP_VERSION = "2026.6.12"
APP_ENGINE = "Atlas V14"
# Short, human changelog of the most recent patches (newest first).
APP_CHANGELOG = [
    "2026.6.12 — CUSDEC item rescue also derives invoice_price (Σ qty×unit) so the CIF closure uses the right basis — ro3 now fully reconciles (gap 0, balanced)",
    "2026.6.11 — CUSDEC item rescue: when the LLM item lines don't reconcile but the CUSDEC's do, adopt the authoritative CUSDEC items (name/HS/qty/price/value)",
    "2026.6.10 — CUSDEC tax/total rescue (deterministic MACCS parse fills CD/CT/AT/SF/MF + real total/rate/decl-no) + reconcile tax-completeness gate (no more false 'balanced' when taxes missing)",
    "2026.6.9 — Finish Atlas rebrand on the agent page (router card), pipeline config + login (Maestro/Veritas/Scrivener → Atlas Router/Swift/Vision)",
    "2026.6.8 — Live-log Atlas rebrand (MAESTRO→ATLAS V14, VERITAS→SWIFT, SCRIVENER→VISION) + mid-stage 'extracting…' heartbeat in the agent terminal",
    "2026.6.7 — Prod hardening: verify_token rejects non-access tokens, /docs gated off by default, DEV_MODE off",
    "2026.6.6 — JWT signing secret auto-generated + DB-persisted when JWT_SECRET_KEY unset (secure-by-default deploy)",
    "2026.6.5 — Perf pass (DB indexes, review-queue join, SQL cost rollups, bulk approve) + UX hardening (theme-var review colors, mobile sidebar, error toasts, undo)",
    "2026.6.4 — Atlas declaration-rescue for bundled release-order PDFs",
    "2026.6.3 — Freight / Insurance / Adjustment (CIF build-up) + tighter CIF gate",
    "2026.6.2 — V14-x engine naming (V14 / V14-1 Swift / V14-2 Vision)",
    "2026.6.1 — Claude UI redesign + grouped sidebar + branded login",
]

# ============================================================================
# PDF CONFIGURATION
# ============================================================================

# PDF path — set dynamically by WebSocket handler or CLI
PDF_PATH = None

# ============================================================================
# API CONFIGURATION
# ============================================================================

# OpenRouter API Key
API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# Try to load from .env if not in environment
if not API_KEY:
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                if line.startswith("OPENROUTER_API_KEY"):
                    API_KEY = line.split("=", 1)[1].strip().strip('"')
                    break

# Validate API key at startup
if not API_KEY or API_KEY == "sk-or-v1-your-openrouter-key-here":
    import logging
    logging.error("CRITICAL: OPENROUTER_API_KEY not set or still placeholder — pipeline will fail")
    # Don't crash on import (allows health check), but pipeline will fail at runtime

# ============================================================================
# MODEL CONFIGURATION
# ============================================================================
# Available models via OpenRouter (all support vision/image input):
#
# Budget tier (~$0.01-0.02 per PDF):
#   "anthropic/claude-3-haiku"           — $0.25/$1.25 per M tokens (struggles with comma-separated numbers)
#   "google/gemini-2.5-flash"            — $0.30/$2.50 per M tokens (best value, native Google OCR, handles commas correctly)
#   "google/gemini-3-flash-preview"      — $0.50/$3.00 per M tokens (latest, enhanced data extraction)
#
# Mid tier (~$0.05-0.10 per PDF):
#   "google/gemini-2.5-pro"              — $1.25/$10.00 per M tokens (highest accuracy, 1M context)
#   "google/gemini-3-pro-preview"        — frontier reasoning, 1M context
#
# Premium tier (~$0.15+ per PDF):
#   "anthropic/claude-3.5-sonnet"        — $3.00/$15.00 per M tokens (best for complex layouts)
#   "anthropic/claude-sonnet-4-6"        — latest Claude, strongest vision

OCR_MODEL = "google/gemini-3-flash-preview"
EXTRACTION_MODEL = "google/gemini-3-flash-preview"

# Per-step model override (None = use EXTRACTION_MODEL)
VISION_MODEL = None                      # Uses EXTRACTION_MODEL (gemini-3-flash)
ASSEMBLER_MODEL = None                   # Uses EXTRACTION_MODEL (gemini-3-flash)
VERIFIER_MODEL = "google/gemini-3-flash-preview"  # Same as extractor; ~60% cheaper than Sonnet on confirm/correct task

# ============================================================================
# OUTPUT CONFIGURATION
# ============================================================================

RESULTS_DIR = Path(__file__).parent / "data" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

UPLOAD_FOLDER = Path(__file__).parent / "data" / "uploads"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

# ============================================================================
# PROCESSING CONFIGURATION
# ============================================================================

# OCR resolution (used in step1_split adaptive resolution)
OCR_RESOLUTION = 3

# API timeout (seconds)
API_TIMEOUT = 180

# ============================================================================
# PIPELINE CONFIGURATION
# ============================================================================

# Retry settings
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds (2, 4, 8)

# Self-review model (can be same or different from extraction model)
REVIEW_MODEL = "google/gemini-3-flash-preview"



# ============================================================================
# KEYCLOAK CONFIGURATION (loaded from DB settings table, cached in memory)
# ============================================================================

_kc_cache = {"config": None, "ts": 0}
_KC_CACHE_TTL = 60  # seconds


def get_keycloak_config():
    """
    Returns Keycloak config from DB settings table (cached 60s).
    Returns None if Keycloak is not enabled.
    Env vars override DB settings if set.
    """
    global _kc_cache

    if time.time() - _kc_cache["ts"] < _KC_CACHE_TTL and _kc_cache["config"] is not None:
        return _kc_cache["config"]

    # Env var override
    env_realm = os.getenv("KEYCLOAK_REALM_URL", "")
    if env_realm:
        kc = {
            "realm_url": env_realm,
            "client_id": os.getenv("KEYCLOAK_CLIENT_ID", ""),
            "client_secret": os.getenv("KEYCLOAK_CLIENT_SECRET", ""),
            "admin_role": os.getenv("KEYCLOAK_ADMIN_ROLE", "admin"),
            "jwks_url": f"{env_realm}/protocol/openid-connect/certs",
            "token_url": f"{env_realm}/protocol/openid-connect/token",
            "auth_url": f"{env_realm}/protocol/openid-connect/auth",
            "logout_url": f"{env_realm}/protocol/openid-connect/logout",
            "enabled": True,
        }
        _kc_cache.update({"config": kc, "ts": time.time()})
        return kc

    # Read from DB
    import database
    enabled = database.get_setting("keycloak_enabled")
    if enabled != "true":
        _kc_cache.update({"config": None, "ts": time.time()})
        return None

    realm_url = database.get_setting("keycloak_realm_url") or ""

    kc = {
        "realm_url": realm_url,
        "client_id": database.get_setting("keycloak_client_id") or "",
        "client_secret": database.get_setting("keycloak_client_secret") or "",
        "admin_role": database.get_setting("keycloak_admin_role") or "admin",
        "jwks_url": f"{realm_url}/protocol/openid-connect/certs",
        "token_url": f"{realm_url}/protocol/openid-connect/token",
        "auth_url": f"{realm_url}/protocol/openid-connect/auth",
        "logout_url": f"{realm_url}/protocol/openid-connect/logout",
        "enabled": True,
    }
    _kc_cache.update({"config": kc, "ts": time.time()})
    return kc


def invalidate_keycloak_cache():
    """Called after settings save to force re-read from DB."""
    global _kc_cache
    _kc_cache = {"config": None, "ts": 0}
