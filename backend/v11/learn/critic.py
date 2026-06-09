#!/usr/bin/env python3
"""
LEARNER — prompt-critic (V11 self-improvement aid)
==================================================

Reviews recent extraction *failures as evidenced by human corrections*
(``field_edits``), clusters them by field + correction pattern, and PROPOSES
prompt-rule improvements that a human can review and (optionally) fold into the
extraction prompts.

Design notes — this module is HUMAN-IN-THE-LOOP and AUDITABLE
-------------------------------------------------------------
* **Proposes, never applies.** This module produces *suggestions only*. It does
  NOT edit any prompt, write any rule, or change pipeline behaviour. A human
  reads :func:`report` and decides whether to act. Nothing here is automatic.
* **No LLM.** Every detection is a deterministic heuristic (regex / string /
  ratio checks) over the human-edit audit trail. There is no model call,
  network call, or randomness — the same DB yields the same suggestions.
* **Auditable.** Every suggestion carries an ``evidence_count`` and up to three
  concrete ``examples`` ({old, new}) drawn straight from ``field_edits`` so a
  reviewer can trace each proposal back to the corrections that motivated it.
* **Fails safe.** All DB access is wrapped in try/except. On an empty DB,
  missing tables, or any driver error this module degrades to ``[]`` /
  ``"(no suggestions / no data)"`` — it must never raise.

DB layer
--------
Uses the same sqlite3-compatible shim as the rest of the backend
(``database._connect()`` → psycopg pooled connection). SQL uses ``?``
placeholders (auto-translated to ``%s``) and ``conn.row_factory = sqlite3.Row``
for dict-like rows. The human corrections live in ``field_edits`` (columns
``field_name``, ``original_value``, ``corrected_value``, ``entity_type`` …).

CLI
---
    python -m v11.learn.critic        # prints report() (safe on empty DB)
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# DB layer — imported lazily-safe. If the import itself fails (e.g. running in a
# context with no DB libs), every public function still degrades gracefully.
try:  # pragma: no cover - import guard
    import database  # type: ignore
    from database import sqlite3  # the _SqliteShim (sqlite3.Row sentinel)
except Exception:  # pragma: no cover
    database = None  # type: ignore
    sqlite3 = None  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────

#: Minimum number of corrections on a field before we propose a rule for it.
MIN_SIGNAL = 3

#: A correction pattern must cover at least this fraction of a field's edits to
#: count as the field's DOMINANT pattern.
DOMINANT_FRACTION = 0.5

#: Two numeric values are treated as "scaled by a large factor" if their ratio
#: (max/min) falls within this band — catches THB/USD (~36x) rate confusion and
#: similar order-of-magnitude unit mix-ups.
SCALE_FACTOR_MIN = 5.0
SCALE_FACTOR_MAX = 5000.0

#: Field-name substrings that hint a field is a country / origin field.
_COUNTRY_FIELD_HINTS = ("origin", "country", "nationality", "consignment_country")

#: Field-name substrings that hint a field is numeric / monetary.
_NUMERIC_FIELD_HINTS = (
    "price", "value", "rate", "amount", "tax", "duty", "fee",
    "quantity", "weight", "total", "ct", "at", "sf", "mf",
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers (pure / deterministic)
# ─────────────────────────────────────────────────────────────────────────────

_ISO2_RE = re.compile(r"^[A-Za-z]{2}$")
_NUMERIC_CHARS_RE = re.compile(r"[^0-9.\-]")

# A small, deterministic country-name → presence check. We don't need the ISO
# code itself — only to recognize that the OLD value looked like a country NAME
# (multiple letters / a word) while the NEW value is a 2-letter code.
_COUNTRY_NAMEY_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{2,}$")


def _s(value) -> str:
    """Coerce any cell value to a stripped string ('' for None)."""
    if value is None:
        return ""
    return str(value).strip()


def _to_float(value) -> Optional[float]:
    """Best-effort float coercion. Returns None for null/blank/garbage.

    Tolerates commas, currency symbols, and stray spaces (edited values are
    messy free text). Returns None for non-positive / unparsable input.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if f != 0 else None
    s = _s(value).replace(",", "")
    s = _NUMERIC_CHARS_RE.sub("", s)
    if not s or s in {"-", ".", "-.", "--"}:
        return None
    try:
        f = float(s)
    except (ValueError, TypeError):
        return None
    return f if f != 0 else None


def _field_looks_country(field_name: str) -> bool:
    f = (field_name or "").lower()
    return any(h in f for h in _COUNTRY_FIELD_HINTS)


def _field_looks_numeric(field_name: str) -> bool:
    f = (field_name or "").lower()
    return any(h in f for h in _NUMERIC_FIELD_HINTS)


def _is_iso2(value: str) -> bool:
    """True if value is exactly a 2-letter alpha code (e.g. 'CN', 'TH')."""
    return bool(_ISO2_RE.match(_s(value)))


def _is_country_namey(value: str) -> bool:
    """True if value reads like a country NAME (a word of >=3 letters)."""
    s = _s(value)
    if len(s) < 3:
        return False
    return bool(_COUNTRY_NAMEY_RE.match(s))


def _scale_ratio(old: Optional[float], new: Optional[float]) -> Optional[float]:
    """Return max/min ratio of two positive numbers, else None."""
    if old is None or new is None:
        return None
    a, b = abs(old), abs(new)
    if a <= 0 or b <= 0:
        return None
    hi, lo = (a, b) if a >= b else (b, a)
    if lo <= 0:
        return None
    return hi / lo


# ─────────────────────────────────────────────────────────────────────────────
# Pattern detectors — each returns (pattern_key, suggestion_text) or None
# ─────────────────────────────────────────────────────────────────────────────

def _detect_country_to_iso(field: str, edits: List[dict]) -> Optional[tuple]:
    """NAME → 2-letter ISO code. e.g. 'China' → 'CN'."""
    hits = 0
    for e in edits:
        old, new = _s(e.get("old")), _s(e.get("new"))
        if not new:
            continue
        if _is_iso2(new) and (_is_country_namey(old) or not _is_iso2(old)):
            hits += 1
    if hits == 0:
        return None
    frac = hits / len(edits)
    if frac < DOMINANT_FRACTION:
        return None
    suggestion = (
        f"Field '{field}': reviewers repeatedly rewrote the value to a 2-letter "
        f"ISO-3166 alpha-2 country code. PROPOSED RULE — instruct the extractor "
        f"to emit the ISO-2 country code (e.g. CN, TH, SG), not the spelled-out "
        f"country name, for '{field}'."
    )
    return ("name_to_iso2_country_code", suggestion, hits)


def _detect_large_scale(field: str, edits: List[dict]) -> Optional[tuple]:
    """Numeric value repeatedly rescaled by a large factor (unit/rate mix-up)."""
    if not _field_looks_numeric(field):
        return None
    ratios: List[float] = []
    for e in edits:
        r = _scale_ratio(_to_float(e.get("old")), _to_float(e.get("new")))
        if r is not None and SCALE_FACTOR_MIN <= r <= SCALE_FACTOR_MAX:
            ratios.append(r)
    if not ratios:
        return None
    frac = len(ratios) / len(edits)
    if frac < DOMINANT_FRACTION:
        return None
    typical = sorted(ratios)[len(ratios) // 2]  # median-ish, deterministic
    suggestion = (
        f"Field '{field}': reviewers repeatedly rescaled the value by ~"
        f"{typical:.1f}x (median of {len(ratios)} corrections). This is the "
        f"classic currency/unit confusion (e.g. THB vs USD exchange rate). "
        f"PROPOSED RULE — instruct the extractor to sanity-check "
        f"invoice_price × exchange_rate ≈ total_customs_value when choosing the "
        f"exchange rate / numeric scale for '{field}', and prefer the rate that "
        f"makes that identity hold."
    )
    return (f"large_scale_factor_~{typical:.0f}x", suggestion, len(ratios))


def _detect_blanked(field: str, edits: List[dict]) -> Optional[tuple]:
    """Field repeatedly cleared/blanked (old non-empty → new empty)."""
    hits = 0
    for e in edits:
        old, new = _s(e.get("old")), _s(e.get("new"))
        if old and not new:
            hits += 1
    if hits == 0:
        return None
    frac = hits / len(edits)
    if frac < DOMINANT_FRACTION:
        return None
    suggestion = (
        f"Field '{field}': reviewers repeatedly CLEARED an extracted value "
        f"(set it back to blank) — the extractor is over-producing / "
        f"hallucinating content here. PROPOSED RULE — tighten the instruction "
        f"for '{field}': only populate it when the value is clearly present on "
        f"the page; otherwise leave it empty rather than guessing."
    )
    return ("repeatedly_blanked", suggestion, hits)


#: Detectors run in priority order; the first that fires wins for a field.
_DETECTORS = (
    _detect_country_to_iso,
    _detect_large_scale,
    _detect_blanked,
)


# ─────────────────────────────────────────────────────────────────────────────
# DB read
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_edits(limit: int) -> List[dict]:
    """Read recent human corrections from ``field_edits``.

    Returns a list of ``{"field": str, "old": str|None, "new": str|None}`` dicts,
    most-recent first. Degrades to ``[]`` on empty DB / missing table / any
    driver error (never raises).
    """
    if database is None:
        return []
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = 500
    if lim <= 0:
        lim = 500

    conn = None
    try:
        conn = database._connect()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT field_name      AS field,
                   original_value  AS old,
                   corrected_value AS new
            FROM field_edits
            ORDER BY id DESC
            LIMIT ?
            """,
            (lim,),
        )
        rows = [dict(r) for r in cursor.fetchall()]
    except Exception as exc:  # missing table / empty DB / driver error
        logger.debug("critic._fetch_edits failed: %s", exc)
        rows = []
    finally:
        _safe_close(conn)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def analyze(limit: int = 500) -> list:
    """Cluster recent human corrections and PROPOSE prompt-rule improvements.

    Reads up to ``limit`` recent rows from ``field_edits``, groups them by
    ``field_name``, and for each field with at least :data:`MIN_SIGNAL`
    corrections detects the single DOMINANT correction pattern via deterministic
    heuristics:

      * **country NAME → ISO-2 code** — suggest emitting ISO-2 country codes;
      * **numeric value rescaled by a large factor** — suggest verifying
        ``invoice_price × exchange_rate ≈ total`` when picking the rate;
      * **field repeatedly blanked** — suggest tightening that field's
        instruction so it isn't over-produced.

    Returns a list of suggestion dicts (one per field that fired)::

        {"field": str, "pattern": short str, "suggestion": str,
         "evidence_count": int, "examples": [up to 3 {"old", "new"}]}

    These are PROPOSALS requiring human approval — nothing is applied. Returns
    ``[]`` on an empty DB or any error; never raises.
    """
    try:
        edits = _fetch_edits(limit)
        if not edits:
            return []

        # Group corrections by field name.
        by_field: Dict[str, List[dict]] = {}
        for e in edits:
            field = _s(e.get("field"))
            if not field:
                continue
            by_field.setdefault(field, []).append(
                {"old": e.get("old"), "new": e.get("new")}
            )

        suggestions: List[dict] = []
        for field, group in sorted(by_field.items()):
            if len(group) < MIN_SIGNAL:
                continue
            for detector in _DETECTORS:
                try:
                    result = detector(field, group)
                except Exception as exc:  # a bad row must not kill the run
                    logger.debug("critic detector failed for %s: %s", field, exc)
                    result = None
                if not result:
                    continue
                pattern, suggestion_text, evidence_count = result
                examples = _pick_examples(group, pattern)
                suggestions.append(
                    {
                        "field": field,
                        "pattern": pattern,
                        "suggestion": suggestion_text,
                        "evidence_count": int(evidence_count),
                        "examples": examples,
                    }
                )
                break  # first (highest-priority) matching detector wins

        # Stable, deterministic ordering: strongest evidence first, then field.
        suggestions.sort(key=lambda s: (-s["evidence_count"], s["field"]))
        return suggestions
    except Exception as exc:  # absolute backstop — never raise
        logger.debug("critic.analyze failed: %s", exc)
        return []


def _pick_examples(group: List[dict], pattern: str) -> List[dict]:
    """Up to 3 representative {old, new} examples for the detected pattern.

    Deterministic: filters the group to rows that actually match the pattern
    (so the evidence shown is relevant), preserving original order.
    """
    def _matches(e: dict) -> bool:
        old, new = _s(e.get("old")), _s(e.get("new"))
        if pattern == "name_to_iso2_country_code":
            return _is_iso2(new)
        if pattern.startswith("large_scale_factor"):
            r = _scale_ratio(_to_float(e.get("old")), _to_float(e.get("new")))
            return r is not None and SCALE_FACTOR_MIN <= r <= SCALE_FACTOR_MAX
        if pattern == "repeatedly_blanked":
            return bool(old) and not new
        return True

    out: List[dict] = []
    for e in group:
        if _matches(e):
            out.append({"old": _s(e.get("old")), "new": _s(e.get("new"))})
            if len(out) >= 3:
                break
    # Fallback: if nothing matched (shouldn't happen), show first few raw.
    if not out:
        for e in group[:3]:
            out.append({"old": _s(e.get("old")), "new": _s(e.get("new"))})
    return out


def report(limit: int = 500) -> str:
    """Render :func:`analyze` as a human-readable review report.

    The report is what an admin reads before deciding whether to fold any rule
    into the prompts. It makes explicit that every line is a PROPOSAL requiring
    human approval and lists the evidence behind each one.

    Returns ``"(no suggestions / no data)"`` when there is nothing to propose.
    Never raises.
    """
    try:
        suggestions = analyze(limit)
    except Exception as exc:  # pragma: no cover - analyze already guards
        logger.debug("critic.report failed: %s", exc)
        suggestions = []

    if not suggestions:
        return "(no suggestions / no data)"

    lines: List[str] = []
    lines.append("=" * 72)
    lines.append("PROMPT-CRITIC — PROPOSED PROMPT-RULE IMPROVEMENTS")
    lines.append("=" * 72)
    lines.append("")
    lines.append(
        "These are PROPOSALS ONLY, derived deterministically from human "
        "corrections in field_edits."
    )
    lines.append(
        "Nothing here has been applied. A human must review and approve before "
        "any rule is added to the prompts."
    )
    lines.append(f"Total proposals: {len(suggestions)}")
    lines.append("")

    for i, s in enumerate(suggestions, 1):
        lines.append("-" * 72)
        lines.append(f"[{i}] FIELD: {s['field']}")
        lines.append(f"    PATTERN : {s['pattern']}")
        lines.append(f"    EVIDENCE: {s['evidence_count']} correction(s)")
        lines.append(f"    PROPOSAL: {s['suggestion']}")
        if s["examples"]:
            lines.append("    EXAMPLES (old → new):")
            for ex in s["examples"]:
                old = ex.get("old") or "(blank)"
                new = ex.get("new") or "(blank)"
                lines.append(f"        - {old!r} → {new!r}")
        lines.append("")

    lines.append("-" * 72)
    lines.append(
        "ACTION: review each proposal; if sound, a human may add the rule to the "
        "relevant prompt. This tool will not do it for you."
    )
    lines.append("=" * 72)
    return "\n".join(lines)


def _safe_close(conn) -> None:
    """Close a connection without ever raising."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    # Import-clean and safe on an empty DB: prints the no-data message rather
    # than raising. Proposes nothing — just renders the review report.
    try:
        print(report())
    except Exception as exc:  # absolute backstop
        logger.debug("critic __main__ failed: %s", exc)
        print("(no suggestions / no data)")
