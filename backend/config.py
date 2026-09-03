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
APP_VERSION = "2026.9.2"
APP_ENGINE = "Atlas V14"

# ============================================================================
# BUILD IDENTITY — the commit this image was built from.
# ============================================================================
# The version string alone is not enough to tell two deployments apart, and
# trusting it cost a month. On 3 Sep 2026 production answered `version:
# 2026.6.16` against a repo at `2026.6.23` — which reads as one small patch
# behind. It was seven weeks and nine commits behind, missing a whole module
# (`v11/textlayer_header.py`), and the only way to establish that was to `ls`
# inside the running container.
#
# `APP_VERSION` is a human label and someone will forget to bump it — this one
# sat unchanged from 17 Jul to 3 Sep while the engine was retired, the evidence
# layer landed and three data-corrupting guards were fixed. The commit cannot be
# forgotten, because the build stamps it: `--build-arg GIT_SHA=$(git rev-parse
# --short HEAD)` in the Dockerfile. "unknown" means the image was built without
# it, which is itself worth seeing.
GIT_SHA = os.getenv("GIT_SHA", "unknown")

# Short, human changelog of the most recent patches (newest first).
APP_CHANGELOG = [
    "2026.9.2 — The PDF pane scrolls. Every page of a bundle is now mounted in one column with its own scrollbar, so a declaration and the continuation sheet holding its item block can be read in one movement instead of a click and a wait per page; the page strip, the prev/next buttons and every field jump still work, they scroll instead of swapping the image, and the page counter is now read FROM the scroll position rather than driving it. Each page measures its own dimensions, because a bundle mixes A4 text pages with photographs of another size and one shared measurement placed a page's highlight boxes using another page's geometry. Pages load lazily so an 18-page bundle does not fetch 18 renders to show the first one. And a page that cannot be drawn now says PAGE N UNAVAILABLE with the reason, at the size a page would have been: the browser's own broken-image alt text is a few narrow words, so a job whose PDF has moved used to collapse the column into a run of text and read as a broken layout rather than a missing file.",
    "2026.9.1 — Three guards were destroying values they were meant to protect, found by re-running the seven documents the team filed complaints about (2 Sep) through the real pipeline and checking every field against the form's own text layer. (1) Phase 4.38 treated a genuine zero as a copied neighbour: on a clean declaration the customs duty and the Exemption/Reduction are BOTH printed as 0, and equality between them carries no information — two documents stored a NULL duty against a printed 0, which tells a reviewer 'nobody could read this' when the form says zero. (2) Phase 4.365 corrects a total misread under the customs PASS stamp using the item block, but never asked where the total came from; on one document the text layer had read `(10) Total customs value 109,138,893.66` EXACTLY and the corroborator overwrote the evidence with the rows it was meant to check — reconcile then reported balanced, having compared the replacement against the numbers that produced it. It now skips a total that was read deterministically; a model-read total is still corrected, and a blank still adopts the item sum. (3) The three invoice columns held three different quantities on the same declaration — invoice_price 1,603,800 (correct), invoice_price_fc 1,688,358.864 (the price PLUS the adjustment) and invoice_price_mmk 109,138,893.66 (the total customs value) — and nothing caught it because the CIF gate reads invoice_price_fc FIRST, so the smuggled build-up double-counts the adjustment and the identity still closes. The invoice price is now read off the page by label position, and Phase 4.38 rejects an MMK invoice price equal to the assessed total. Verified: 47 of 47 fields across all seven documents match the forms. /api/health now reports the build's git commit, because the version string alone could not distinguish a two-month-old deployment from a current one.",
    "2026.8.4 — Document identity: a bundled release order carries an Import Licence beside the customs declaration, with its own goods table, the same HS codes and its own Total CIF Value — so a read of the licence is complete, self-consistent and passes every arithmetic gate. One document stored 19 product rows for a four-item declaration, 11 of them reconciling to the penny against the wrong paper. The page classifier now returns a `document` axis (DECLARATION / LICENCE / INVOICE / PACKING_LIST) separately from how a page is filled in — 'typed' says a page is machine-printed, not that it is authoritative — a lane that never read a declaration page contributes no items, reconcile folds a document check into `balanced`, and item dedup normalisation lands last so duplicates stay visible until provenance is right. Verified on the real document: 19 items → 4, correct quantities, cost $0.2578 → $0.0849. Lifecycle dates are deterministic-only: on a scanned page the model filled a blank arrival date by echoing a neighbouring one, and a date has no arithmetic to fail, so blank now beats an echo.",
    "2026.8.2 — ATLAS V14 is the only extraction engine. Rover Pro, Rosetta, Presto-standalone and Atlas Classic are retired: they returned above every Atlas stage, so none of them saw page scoping, the scanned-CUSDEC vision rescue or the CIF derivation, and on a bundle whose declaration is a photograph their native-PDF reader took the text layer of the Import Licence and the waybill — a different consignment, with nothing downstream able to notice. Retired engine ids are stripped from the stored settings row on read, so an old database value cannot resurrect one. Evidence layer: every value now carries where it was read from and where it sits on the page, with an annotated PDF and a checks queue; a value that cannot be located returns 0 and renders as a dash, because no box beats a wrong box — a reviewer sent to the wrong document to confirm a customs figure is being told something false, and it looks deliberate.",
    "2026.8.1 — Storage types and shared parsers. Money is numeric(20,4), rates numeric(24,10) (a real ledger rate is 61.95007144978846, which `real` stored as 61.95007), and twelve *_json columns became jsonb. Ten near-copies of a number parser silently dropped any amount printed with its currency ('THB 652,279.7184' → None); `numeric.py` and `dates.py` are now the single parsers, and they refuse to strip every non-digit because that turns a date and the MA-series declaration id into numbers. `_pick()` replaced `a or b` on every money row — Commercial Tax is genuinely 0 on many declarations and was being stored NULL. Excel exports now match the team's own workbook exactly, and issues are explained in plain English rather than pipeline jargon.",
    "2026.7.30 — UI revamp: sidebar navigation, search, date-range and filters across history, declarations and items, three-way dark mode. The date filter never drops a row it cannot parse.",
    "2026.6.23 — Self-improvement Phase 7: an evaluation harness that MEASURES whether a change actually helps before adopting it (ALMA-inspired assess→measure→archive→adopt loop). v11/learn/evaluate.py replays the golden corpus (approved jobs = ground truth) through Presto/Scribe, scores each field vs the approved value (numeric tolerance, ISO-date prefix, string-normalized), computes field_accuracy + item_recall + per-field breakdown, and honestly counts records whose source PDF can't be located as skipped (never faked). A scored archive + promote_if_better() gate adopt a candidate config only when it beats the baseline. v11/learn/proposer.py adds a bounded, flag-gated (LEARN_PROPOSER, OpenRouter-only) LLM meta-agent that reads the accumulated signals (weak fields, critic patterns, frequently-corrected fields) and PROPOSES ≤5 general prompt rules — still human-approved, never auto-applied. New admin API: GET /api/learn/evaluate, /evaluate/scores, POST /evaluate/promote, GET /proposals/llm. This closes the loop's missing half: corrections were captured and injected, but nothing measured the lift — now every change is scored against approved truth and rolled back if it regresses. Arithmetic gates still decide truth.",
    "2026.6.22 — Self-improvement loop, Phases 2-6 (the full flywheel, all flag-gated + fail-safe): P2 approving a job now auto-rebuilds that importer's priors (LEARN_AUTO_PRIORS) so the drift-warning read-path goes live instead of needing a manual CLI. P3 the handwriting engine can spend extra cross-model votes on historically-weak fields (LEARN_ADAPTIVE_VOTES, capped by SCRIBE_MAX_VOTES) — wiring the previously-dead weakspots.vote_plan. P4 admin-approved prompt rules: critic.py proposals can be approved (POST /api/learn/rules) and are injected into the Presto/Scribe prompt (LEARN_PROMPT_RULES) — human-gated, never auto-applied. P5 every human correction now records against (importer, field) via a new corrections-only counter, so field_accuracy fills and weakspots error-rates become real (was dead scaffolding). P6 the lost golden corpus is reconstructed from approved jobs (GET /api/learn/golden/export) — every approval is a labelled example, so the regression/bake-off corpus rebuilds itself from production review. New admin surface /api/learn/{status,proposals,rules,weakspots,priors/rebuild,golden/export}. All new behaviour is OFF by default; guardrails: arithmetic gates still decide truth (JUDGE hard-interlock), only human-approved data feeds learning, every path degrades to no-op on error.",
    "2026.6.21 — Self-improvement loop, Phase 1 (closed the feedback gap): the production engines now LEARN from human review. Presto (typed) and Scribe (handwritten) prepend a learned-correction hint block to their primary extraction prompt — a values-free ATTENTION LIST of the fields reviewers most often had to correct (so the model spends extra care exactly where it historically errs), plus per-importer value hints when the importer is known. Sourced only from the human-authored field_edits audit trail, never from model output. Fully flag-gated: OFF by default (LEARN_FEWSHOT_PRIMARY), with a SHADOW mode (LEARN_FEWSHOT_SHADOW) that logs what WOULD be injected without changing extraction, so it can be validated before enabling. Fails safe to no-injection on any DB/learner error — extraction never breaks. Injection is surfaced in the engine diagnostics (presto.fewshot_injected / scribe.fewshot_injected). Until now only the legacy V7 assembler learned from corrections; the Atlas engines every request actually uses had fully static prompts.",
    "2026.6.20 — CUSDEC accuracy hardening from the 15-Jul UAT: (1) Wrong Date — the deterministic declaration-date read now widens the wrong-date guard (Expected/Estimated/Est/Exp/Planned/Provisional/Tentative/Special) AND reads the value from the grid cell BELOW the 'Declaration date' header, not just inline, so it stops locking onto the Expected date; the text-layer fallback also accepts the value printed after the label (exact-match still keeps 'Expected declaration date' out). (2) Missing 2%/5%/Duty taxes — the tax block is matched by keyword now (IMPORT DUTY, COMMERCIAL TAX (5%), INCOME TAX (2%), SERVICE FEE), not exact label, so real-world label variants are no longer dropped; a '-' dash now correctly reads as an absent tax (None) instead of stealing the neighbouring tax's number. (3) Wrong Inv No — the commercial-invoice cleaner only strips a section-code prefix when the pattern sits at the end, so a trailing suffix (e.g. -REV2) is no longer silently dropped. (4) Security — Keycloak audience verification is now opt-in (Settings keycloak_verify_aud / KEYCLOAK_VERIFY_AUD), closing the last flagged gap without risking a lock-out by default. 12 new unit tests pin the date/tax/geometry/invoice behaviour.",
    "2026.6.19 — Document-type triage is now a first-class step: every job is classified once (DIGITAL / SCANNED / MIXED, and specifically whether the CUSDEC page is text or a scan), recorded on the job (jobs.doc_class), shown in the live terminal (TRIAGE event), and used as the single source of truth for routing — so 'why is this slow / flagged?' is answered up front and the digital-vs-scanned signal can't drift between modules. Scanned CUSDEC pages (no text layer, ~2/3 of real docs) now get a targeted single vision read for rate/date/taxes/total instead of silently skipping the deterministic rescue. Golden tests pin the triage decision for the whole sample corpus.",
    "2026.6.18 — Speed + accuracy on CIF/scanned docs: the arithmetic-closure check now uses the full CIF basis (invoice + freight + insurance + adjustment), so a legitimate CIF/DAP uplift no longer false-fires HIGH and drags the doc through the whole (fruitless) arbiter → cell-zoom → cell-zoom-PRO vision cascade — that case is now advisory-only. The reconcile recovery pass (a second full V7 vision run) only fires on a real item-sum shortfall, not a rate/CIF/tax gap. The rate guard now uses the per-currency band as its primary signal and only auto-corrects when the derived rate is trustworthy (full CIF basis present), so a correct CIF rate whose invoice-only derivation looks off is never falsely 'corrected'. Net: unbalanced CIF/scanned docs finish far faster and stop mis-flagging good rates.",
    "2026.6.17 — Exchange-rate guard: the FX rate is now read by page geometry (anchored on the 'Exchange Rate' label row, not the first decimal on the page), the rate regex accepts 4-digit USD rates, and each currency has a sanity band — so USD no longer collapses to 500 and stray customs figures (636.2576) stop masquerading as the rate. reconcile() cross-checks every rate against the math-derived value (total ÷ CIF/item basis): a suspect rate is auto-corrected to the derived value AND always forced to human review (never ships silently). Tax-completeness gate now requires a CORE tax (duty/CT/AT), so a doc carrying only the flat MACCS/security fee no longer counts as 'balanced'. RO/ID declaration date also extracted deterministically.",
    "2026.6.16 — Excel exports now carry Freight/Insurance/Adjustment (both the per-job download and the Declarations export); v13 Scribe shipped in the Docker image (handwritten pages used to crash the worker); tax gate accepts the engines' own field names so the Presto fast-path can actually pass (was always falling back to the slow V7); extract/status/SSE endpoints + the 4 usage endpoints now require auth; review flag no longer auto-set on every handwritten job",
    "2026.6.15 — New Settings → Usage tab: spend/requests/token-volume KPIs, per-user breakdown, spend-by-model chart + table, date-range (week/month/3mo/all). Backed by GET /api/usage/overview (admin).",
    "2026.6.14 — Rebrand to 'City Agent : PG Release Order' (tab title + CityAgent favicon + in-app RO-ED labels + API title); internal storage/pipeline keys unchanged",
    "2026.6.13 — CUSDEC rescue also reads freight/insurance/adjustment (real number wins; '-' dash stays blank, explicit 0 shows as 0 not —)",
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
            "verify_aud": os.getenv("KEYCLOAK_VERIFY_AUD", "").lower() == "true",
            "expected_audience": os.getenv("KEYCLOAK_AUDIENCE", "") or os.getenv("KEYCLOAK_CLIENT_ID", ""),
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
        "verify_aud": (database.get_setting("keycloak_verify_aud") == "true")
                      or (os.getenv("KEYCLOAK_VERIFY_AUD", "").lower() == "true"),
        "expected_audience": (database.get_setting("keycloak_audience")
                              or os.getenv("KEYCLOAK_AUDIENCE", "")
                              or database.get_setting("keycloak_client_id") or ""),
        "enabled": True,
    }
    _kc_cache.update({"config": kc, "ts": time.time()})
    return kc


def invalidate_keycloak_cache():
    """Called after settings save to force re-read from DB."""
    global _kc_cache
    _kc_cache = {"config": None, "ts": 0}
