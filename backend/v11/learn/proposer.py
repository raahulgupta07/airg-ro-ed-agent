#!/usr/bin/env python3
"""
LEARNER — LLM prompt-rule PROPOSER (V11 self-improvement, ALMA-style ideation)
=============================================================================

A *bounded* LLM meta-agent that reads the accumulated learning signals and asks
a model to propose candidate prompt-improvement rules for the Myanmar-customs
declaration extractor. This is the "ideation" step in an ALMA-style loop:

    LLM proposes  →  human reviews/approves  →  only THEN is a rule applied.

Design contract — read this before touching anything here
---------------------------------------------------------
* **Proposes, never applies.** This module returns *candidate rules only*. It
  writes NOTHING to the DB, edits NO prompt, and changes NO pipeline behaviour.
  Approval flows through the existing ``rules.approve_rule`` path, driven by a
  human. Nothing here is automatic.
* **OpenRouter ONLY.** The single model call is a plain HTTPS POST (``requests``)
  to the OpenRouter chat-completions endpoint using ``config.API_KEY`` and
  ``config.EXTRACTION_MODEL``. No openai / anthropic / google SDK is imported.
* **Flag-gated — no accidental spend.** The network call only happens when the
  env flag ``LEARN_PROPOSER`` is truthy. Off (the default) ⇒ every entry point
  returns ``[]`` immediately, before any HTTP is attempted. Costs money only
  when an operator explicitly turns it on.
* **Fails safe.** Every public function is wrapped in try/except and degrades to
  a safe empty value (``[]`` / ``{}`` / a "(nothing)" string). It never raises,
  even with no DB, no network, or a malformed model response.
* **Evidence-driven.** The proposer reasons only over the deterministic learning
  signals (``weakspots``, ``critic``, ``fewshot``). It is asked for GENERAL
  rules, not memorised one-document values.

DB layer
--------
Only touched indirectly, through the sibling learn modules (each already
fail-safe). ``database`` is imported behind a guard so this module imports even
with no DB / DB libs present.

CLI
---
    python -m v11.learn.proposer     # prints report() (safe with flag off / no data)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── Config (OpenRouter creds + default model). Guarded so import never fails. ──
try:  # pragma: no cover - import guard
    import config  # type: ignore
except Exception:  # pragma: no cover
    config = None  # type: ignore

# HTTP client — guarded so a bare/minimal env still imports (flag-off path needs
# no network anyway).
try:  # pragma: no cover - import guard
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore

# DB layer — imported lazily-safe (kept for parity with sibling learn modules;
# this module reaches the DB only through them, never directly).
try:  # pragma: no cover - import guard
    import database  # type: ignore
    from database import sqlite3  # noqa: F401  (the _SqliteShim row sentinel)
except Exception:  # pragma: no cover
    database = None  # type: ignore
    sqlite3 = None  # type: ignore

# Sibling learn modules — each is itself fail-safe. Guarded so a missing/renamed
# module degrades this proposer to "no signal" rather than an ImportError.
try:  # pragma: no cover - import guard
    from v11.learn import weakspots  # type: ignore
except Exception:  # pragma: no cover
    weakspots = None  # type: ignore
try:  # pragma: no cover - import guard
    from v11.learn import critic  # type: ignore
except Exception:  # pragma: no cover
    critic = None  # type: ignore
try:  # pragma: no cover - import guard
    from v11.learn import fewshot  # type: ignore
except Exception:  # pragma: no cover
    fewshot = None  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────

API_URL = "https://openrouter.ai/api/v1/chat/completions"

#: Hard cap on how many rules we ever return — keep the human review load small.
MAX_RULES = 5

#: Model call budget.
_MAX_TOKENS = 1500
_TIMEOUT = 120
_RETRIES = 3

_TRUTHY = {"1", "true", "yes", "on"}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _flag(name: str) -> bool:
    """True iff env var ``name`` is set to a truthy value ("1"/"true"/"yes"/"on").

    The proposer's whole cost-safety rests on this: with the flag unset (the
    default) no model call is ever made. Never raises.
    """
    try:
        return str(os.getenv(name, "")).strip().lower() in _TRUTHY
    except Exception:  # pragma: no cover - defensive
        return False


def _s(value) -> str:
    """Coerce any value to a stripped string ('' for None)."""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # pragma: no cover
        return ""


def _model() -> Optional[str]:
    """Default extraction model from config, or None if config is unavailable."""
    try:
        return getattr(config, "EXTRACTION_MODEL", None)
    except Exception:  # pragma: no cover
        return None


def _api_key() -> str:
    """OpenRouter API key from config, or '' if unavailable."""
    try:
        return _s(getattr(config, "API_KEY", ""))
    except Exception:  # pragma: no cover
        return ""


def _has_signal(signals: dict) -> bool:
    """True if there is ANY evidence to reason over (else nothing to learn)."""
    if not isinstance(signals, dict):
        return False
    return any(bool(signals.get(k)) for k in (
        "weak_fields", "error_rates", "critic_proposals", "frequently_corrected",
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Signal gathering
# ─────────────────────────────────────────────────────────────────────────────

def gather_signals(limit: int = 200) -> dict:
    """Assemble the evidence the proposer reasons over, from the learn modules.

    Every sub-call is fail-safe and may legitimately be empty (fresh DB). The
    returned dict always has the four keys, each defaulting to an empty
    container. Never raises.

    Keys:
      * ``weak_fields``          — [field, …] worst-first (weakspots.weak_fields)
      * ``error_rates``          — {field: rate 0..1} (weakspots.field_error_rates)
      * ``critic_proposals``     — [{field,pattern,suggestion,evidence_count,
                                     examples}, …] (critic.analyze)
      * ``frequently_corrected`` — [(field, times_corrected), …] (fewshot)
    """
    out = {
        "weak_fields": [],
        "error_rates": {},
        "critic_proposals": [],
        "frequently_corrected": [],
    }

    # weakspots — weak fields + per-field error rates
    if weakspots is not None:
        try:
            wf = weakspots.weak_fields()
            if isinstance(wf, list):
                out["weak_fields"] = wf
        except Exception as exc:  # pragma: no cover - sub-call already guards
            logger.debug("proposer.gather_signals weak_fields failed: %s", exc)
        try:
            er = weakspots.field_error_rates()
            if isinstance(er, dict):
                out["error_rates"] = er
        except Exception as exc:  # pragma: no cover
            logger.debug("proposer.gather_signals field_error_rates failed: %s", exc)

    # critic — deterministic clustered correction proposals
    if critic is not None:
        try:
            cp = critic.analyze(limit=limit)
            if isinstance(cp, list):
                out["critic_proposals"] = cp
        except Exception as exc:  # pragma: no cover
            logger.debug("proposer.gather_signals critic.analyze failed: %s", exc)

    # fewshot — most-corrected fields across all importers
    if fewshot is not None:
        try:
            fc = fewshot.frequently_corrected_fields()
            if isinstance(fc, list):
                out["frequently_corrected"] = fc
        except Exception as exc:  # pragma: no cover
            logger.debug(
                "proposer.gather_signals frequently_corrected_fields failed: %s", exc)

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Prompt building
# ─────────────────────────────────────────────────────────────────────────────

def _build_prompt(signals: dict) -> str:
    """Render a concise instruction asking the model to propose <=5 SHORT rules.

    The model is told the rules must be GENERAL (not a memorised value), a single
    imperative sentence each, and returned as STRICT JSON. The gathered signals
    are embedded as evidence. Pure string building — never raises.
    """
    if not isinstance(signals, dict):
        signals = {}

    lines: List[str] = []
    lines.append(
        "You are an expert prompt engineer improving an automated extractor for "
        "Myanmar customs declaration (CUSDEC / release-order) PDFs. The extractor "
        "reads a document and returns a structured declaration header plus line "
        "items. Human reviewers correct its mistakes; the correction patterns "
        "below are the evidence of where it goes wrong."
    )
    lines.append("")
    lines.append("=== EVIDENCE ===")

    # Weak fields
    weak = signals.get("weak_fields") or []
    rates = signals.get("error_rates") or {}
    if weak:
        annotated = []
        for f in weak[:20]:
            fk = _s(f)
            if not fk:
                continue
            try:
                r = rates.get(fk)
            except Exception:
                r = None
            if isinstance(r, (int, float)):
                annotated.append(f"{fk} (~{float(r) * 100:.0f}% correction rate)")
            else:
                annotated.append(fk)
        if annotated:
            lines.append("")
            lines.append("Fields with the highest human-correction rates:")
            lines.append("  " + ", ".join(annotated))

    # Frequently corrected
    freq = signals.get("frequently_corrected") or []
    if freq:
        parts = []
        for entry in freq[:20]:
            try:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    parts.append(f"{_s(entry[0])} ({int(entry[1])}x)")
                else:
                    parts.append(_s(entry))
            except Exception:
                continue
        parts = [p for p in parts if p]
        if parts:
            lines.append("")
            lines.append("Fields reviewers correct most often (count of corrections):")
            lines.append("  " + ", ".join(parts))

    # Critic proposals — deterministic patterns + concrete examples
    crit = signals.get("critic_proposals") or []
    if crit:
        lines.append("")
        lines.append("Observed correction patterns (clustered from human edits):")
        for i, cp in enumerate(crit[:10], 1):
            if not isinstance(cp, dict):
                continue
            field = _s(cp.get("field"))
            pattern = _s(cp.get("pattern"))
            suggestion = _s(cp.get("suggestion"))
            evc = cp.get("evidence_count")
            head = f"  {i}. field='{field}' pattern='{pattern}'"
            if isinstance(evc, (int, float)):
                head += f" ({int(evc)} corrections)"
            lines.append(head)
            if suggestion:
                lines.append(f"     note: {suggestion}")
            examples = cp.get("examples") or []
            if isinstance(examples, list) and examples:
                ex_bits = []
                for ex in examples[:3]:
                    if not isinstance(ex, dict):
                        continue
                    old = _s(ex.get("old")) or "(blank)"
                    new = _s(ex.get("new")) or "(blank)"
                    ex_bits.append(f"{old!r}->{new!r}")
                if ex_bits:
                    lines.append("     examples (old->corrected): " + "; ".join(ex_bits))

    lines.append("")
    lines.append("=== YOUR TASK ===")
    lines.append(
        "Propose AT MOST 5 short, concrete prompt rules that, if added to the "
        "extractor's instructions, would reduce the correction patterns above."
    )
    lines.append("Each rule MUST:")
    lines.append(
        "  - be GENERAL — a reusable instruction about how to read/normalise a "
        "field, NOT a memorised value from one specific document;")
    lines.append("  - be a SINGLE imperative sentence;")
    lines.append(
        "  - name the field it targets when it is field-specific (else field=null);")
    lines.append("  - be genuinely actionable for the extractor.")
    lines.append("")
    lines.append("Return STRICT JSON and nothing else, in exactly this shape:")
    lines.append(
        '{"rules":[{"field": <string or null>, "text": <string>, '
        '"rationale": <string>}]}')
    lines.append(
        "'text' is the imperative rule; 'rationale' briefly cites which evidence "
        "it addresses. Return {\"rules\":[]} if you have no confident proposal.")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# JSON parsing (tolerant)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    """Best-effort parse of a model response into a dict. Never raises.

    Strips ``` fences, then falls back to the first '{' … last '}' slice.
    Returns {} on any failure.
    """
    s = _s(raw)
    if not s:
        return {}
    # Strip code fences (```json ... ``` or ``` ... ```).
    if "```" in s:
        s = re.sub(r"```[a-zA-Z0-9_]*", "", s).replace("```", "").strip()
    # Direct attempt.
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # Fallback: first { … last }.
    try:
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            obj = json.loads(s[start:end + 1])
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return {}


def _normalize_rules(parsed: dict) -> list:
    """Extract + normalize the ``rules`` list to [{field,text,rationale}], capped.

    Drops entries with no ``text``. ``field`` is a string or None. Never raises.
    """
    if not isinstance(parsed, dict):
        return []
    raw_rules = parsed.get("rules")
    if not isinstance(raw_rules, list):
        return []

    out: List[dict] = []
    for entry in raw_rules:
        if not isinstance(entry, dict):
            continue
        text = _s(entry.get("text"))
        if not text:
            continue
        field_raw = entry.get("field")
        field = _s(field_raw) or None
        rationale = _s(entry.get("rationale"))
        out.append({"field": field, "text": text, "rationale": rationale})
        if len(out) >= MAX_RULES:
            break
    return out


# ─────────────────────────────────────────────────────────────────────────────
# The meta-agent
# ─────────────────────────────────────────────────────────────────────────────

def propose_rules(limit: int = 200, model: str = None) -> list:
    """Ask the LLM to PROPOSE prompt-improvement rules from the learning signals.

    Bounded + cost-safe:
      * returns ``[]`` immediately unless env ``LEARN_PROPOSER`` is truthy (so it
        never spends money by accident);
      * returns ``[]`` when there is no learning signal at all (nothing to learn);
      * makes a single OpenRouter call (``config.EXTRACTION_MODEL`` by default),
        temperature 0, JSON-object response, up to 3 retries with backoff.

    Returns a list of ``{"field","text","rationale"}`` PROPOSALS (capped at 5).
    These require human approval via the existing ``rules.approve_rule`` path —
    this function writes NOTHING. Returns ``[]`` on any failure; never raises.
    """
    try:
        # 1) Cost gate — no flag, no spend.
        if not _flag("LEARN_PROPOSER"):
            return []

        # 2) Evidence gate — nothing to reason over ⇒ don't call the model.
        signals = gather_signals(limit)
        if not _has_signal(signals):
            return []

        # 3) Preconditions for a network call.
        if requests is None:
            logger.debug("proposer.propose_rules: requests unavailable")
            return []
        api_key = _api_key()
        if not api_key:
            logger.debug("proposer.propose_rules: no API key")
            return []
        use_model = _s(model) or _model()
        if not use_model:
            logger.debug("proposer.propose_rules: no model configured")
            return []

        prompt = _build_prompt(signals)
        payload = {
            "model": use_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": _MAX_TOKENS,
            "response_format": {"type": "json_object"},
        }

        raw = ""
        err = None
        for attempt in range(_RETRIES):
            try:
                r = requests.post(
                    API_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=_TIMEOUT,
                )
                if r.status_code == 200:
                    body = r.json()
                    raw = body["choices"][0]["message"]["content"]
                    break
                elif r.status_code == 429:
                    time.sleep(2 ** (attempt + 1))
                    continue
                else:
                    err = f"HTTP {r.status_code}: {r.text[:200]}"
            except Exception as e:  # network / decode error
                err = str(e)
            if attempt < _RETRIES - 1:
                time.sleep(2 ** (attempt + 1))

        if not raw:
            logger.debug("proposer.propose_rules: no response (%s)", err)
            return []

        rules = _normalize_rules(_parse_json(raw))
        return rules[:MAX_RULES]
    except Exception as exc:  # absolute backstop — never raise
        logger.debug("proposer.propose_rules failed: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Human-readable report
# ─────────────────────────────────────────────────────────────────────────────

def report(limit: int = 200) -> str:
    """Render :func:`propose_rules` as a human-readable review report.

    Makes explicit that every line is a PROPOSAL requiring human approval.
    Returns a "(nothing)" message when the flag is off, there is no signal, or
    the model proposed nothing. Never raises.
    """
    try:
        rules = propose_rules(limit=limit)
    except Exception as exc:  # pragma: no cover - propose_rules already guards
        logger.debug("proposer.report failed: %s", exc)
        rules = []

    if not rules:
        if not _flag("LEARN_PROPOSER"):
            return "(LEARN_PROPOSER is off — no proposals; set the flag to enable)"
        return "(no proposals / no signal)"

    lines: List[str] = []
    lines.append("=" * 72)
    lines.append("PROMPT-PROPOSER — LLM-SUGGESTED PROMPT-RULE IMPROVEMENTS")
    lines.append("=" * 72)
    lines.append("")
    lines.append(
        "These are PROPOSALS ONLY, ideated by an LLM from the deterministic "
        "learning signals (weakspots / critic / fewshot).")
    lines.append(
        "Nothing here has been applied. A human must review and approve each rule "
        "(via rules.approve_rule) before it is added to any prompt.")
    lines.append(f"Total proposals: {len(rules)}")
    lines.append("")

    for i, rule in enumerate(rules, 1):
        field = rule.get("field") or "(general)"
        lines.append("-" * 72)
        lines.append(f"[{i}] FIELD    : {field}")
        lines.append(f"    RULE     : {rule.get('text', '')}")
        rationale = rule.get("rationale")
        if rationale:
            lines.append(f"    RATIONALE: {rationale}")
        lines.append("")

    lines.append("-" * 72)
    lines.append(
        "ACTION: review each proposal; if sound, a human may approve the rule. "
        "This tool will not apply anything for you.")
    lines.append("=" * 72)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI: python -m v11.learn.proposer
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    # Safe with the flag off / no data / no network: prints the "(nothing)"
    # message rather than raising. Applies nothing — only renders the report.
    try:
        print(report())
    except Exception as exc:  # absolute backstop
        logger.debug("proposer __main__ failed: %s", exc)
        print("(no proposals / no signal)")
