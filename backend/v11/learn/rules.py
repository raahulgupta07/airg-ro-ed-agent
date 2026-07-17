#!/usr/bin/env python3
"""LEARNER — human-approved prompt rules (Phase-4 self-improvement).

`critic.py` PROPOSES prompt-rule improvements from clustered human corrections;
this module is the APPROVED side of that loop. An admin reviews a proposal and,
if sound, approves it — the rule text is persisted (settings kv, JSON list) and
from then on rendered into the extractor prompt via `fewshot.primary_hint_block`.

Design contract (same as the rest of learn/):
* **Human-gated.** Nothing here is auto-applied. Rules exist only after an admin
  POSTs an approval (see routes/learn.py). `critic.analyze` never writes here.
* **Advisory prompt text.** Rules are appended as guidance; they never overwrite
  an extracted value or hard-block anything. Arithmetic gates still decide truth.
* **Flag-gated injection.** The rendered block is only injected when
  LEARN_PROMPT_RULES is on (checked by the caller).
* **Fails safe.** Every DB access degrades to []/"" — never raises into the
  extractor or a request handler.

Storage: settings key `learn_prompt_rules` = JSON list of
    {"text": str, "field": str|None, "approved_by": str, "approved_at": str}
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

try:  # degrade gracefully with no DB
    import database  # type: ignore
except Exception:  # pragma: no cover
    database = None  # type: ignore

SETTING_KEY = "learn_prompt_rules"
MAX_RULES = 25  # keep the injected block bounded


def list_approved() -> List[dict]:
    """All approved rules (newest first). ``[]`` on empty/error."""
    if database is None:
        return []
    try:
        raw = database.get_setting(SETTING_KEY)
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.debug("list_approved failed: %s", exc)
        return []


def approve_rule(text: str, field: Optional[str] = None,
                 approved_by: str = "admin") -> dict:
    """Persist a new approved rule. De-dups on exact text. Returns the stored
    record (or the existing one). Raises only if the DB write itself fails."""
    if database is None:
        raise RuntimeError("no database")
    text = " ".join(str(text or "").split()).strip()
    if not text:
        raise ValueError("empty rule text")

    rules = list_approved()
    for r in rules:
        if r.get("text") == text:
            return r  # idempotent

    # timestamp is read from the DB (no Date.now() in this env's helpers); use a
    # DB-provided value if available, else leave blank — ordering is list-order.
    rec = {"text": text, "field": (field or None),
           "approved_by": approved_by, "approved_at": _now()}
    rules.insert(0, rec)
    rules = rules[:MAX_RULES]
    database.set_setting(SETTING_KEY, json.dumps(rules), updated_by=approved_by)
    return rec


def remove_rule(text: str, updated_by: str = "admin") -> bool:
    """Delete an approved rule by exact text. True if something was removed."""
    if database is None:
        return False
    text = " ".join(str(text or "").split()).strip()
    rules = list_approved()
    kept = [r for r in rules if r.get("text") != text]
    if len(kept) == len(rules):
        return False
    database.set_setting(SETTING_KEY, json.dumps(kept), updated_by=updated_by)
    return True


def approved_rules_block() -> str:
    """Render approved rules as a prompt block. ``""`` when none. Caller gates on
    LEARN_PROMPT_RULES before injecting."""
    rules = list_approved()
    if not rules:
        return ""
    lines = ["Extraction rules learned from past reviewer corrections "
             "(admin-approved — follow these):"]
    for r in rules:
        t = (r.get("text") or "").strip()
        if t:
            lines.append(f"  - {t}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n"


def _now() -> str:
    """Best-effort timestamp via the DB (this env forbids Date.now-style calls in
    some contexts). Falls back to ''."""
    try:
        if database is not None:
            conn = database._connect()
            cur = conn.cursor()
            cur.execute("SELECT datetime('now')")
            v = cur.fetchone()
            conn.close()
            return str(v[0]) if v else ""
    except Exception:
        pass
    return ""
