#!/usr/bin/env python3
"""Learn route — self-improvement control surface (admin).

Exposes the LEARNER layer that turns human review into better extraction:
  * proposals  — critic.py clusters corrections → PROPOSED prompt rules (read)
  * rules      — admin approves/removes rules that get injected into the prompt
  * weakspots  — per-field error rates driving adaptive votes (read)
  * priors     — rebuild importer norms from approved jobs (write)
  * golden     — export the ground-truth corpus rebuilt from approved jobs (read)

All endpoints require admin. Everything is advisory/human-gated — nothing here
changes an extracted value; rules only take effect when LEARN_PROMPT_RULES is on.
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from middleware import require_admin

router = APIRouter()


class RuleRequest(BaseModel):
    text: str
    field: Optional[str] = None


class PromoteRequest(BaseModel):
    label: str
    metrics: dict
    baseline: Optional[dict] = None


@router.get("/proposals")
async def get_proposals(limit: int = 500, current_user: dict = Depends(require_admin)):
    """Critic proposals from clustered human corrections (not yet approved)."""
    try:
        from v11.learn import critic
        return {"proposals": critic.analyze(limit=limit)}
    except Exception as e:
        return {"proposals": [], "error": str(e)}


@router.get("/rules")
async def get_rules(current_user: dict = Depends(require_admin)):
    """Currently approved prompt rules (injected when LEARN_PROMPT_RULES=1)."""
    from v11.learn import rules
    return {"rules": rules.list_approved()}


@router.post("/rules")
async def add_rule(req: RuleRequest, current_user: dict = Depends(require_admin)):
    """Approve a prompt rule (from a proposal or hand-written)."""
    from v11.learn import rules
    try:
        rec = rules.approve_rule(req.text, field=req.field,
                                 approved_by=current_user.get("username", "admin"))
        return {"status": "ok", "rule": rec, "rules": rules.list_approved()}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Failed to approve rule: {e}")


@router.delete("/rules")
async def delete_rule(req: RuleRequest, current_user: dict = Depends(require_admin)):
    """Remove an approved rule by exact text."""
    from v11.learn import rules
    removed = rules.remove_rule(req.text, updated_by=current_user.get("username", "admin"))
    return {"status": "ok", "removed": removed, "rules": rules.list_approved()}


@router.get("/weakspots")
async def get_weakspots(importer: Optional[str] = None,
                        current_user: dict = Depends(require_admin)):
    """Per-field error rates (drives adaptive Scribe votes when enabled)."""
    from v11.learn import weakspots
    return {"error_rates": weakspots.field_error_rates(importer),
            "weak_fields": weakspots.weak_fields(importer)}


@router.post("/priors/rebuild")
async def rebuild_priors(importer: Optional[str] = None,
                         current_user: dict = Depends(require_admin)):
    """Rebuild importer priors from approved jobs (all importers if omitted)."""
    from v11.learn import priors
    try:
        result = priors.build_priors(importer)
        return {"status": "ok", "priors": result}
    except Exception as e:
        raise HTTPException(500, f"Failed to build priors: {e}")


@router.get("/golden/export")
async def export_golden(limit: Optional[int] = None,
                        current_user: dict = Depends(require_admin)):
    """The ground-truth corpus reconstructed from approved jobs (Phase 6).
    Regenerates the lost golden set from real production review."""
    from v11.learn import golden
    return golden.build_golden(limit=limit)


@router.get("/evaluate")
async def evaluate_golden(engine: str = "presto", limit: Optional[int] = None,
                         current_user: dict = Depends(require_admin)):
    """Score the current pipeline against the golden corpus (admin-triggered).

    Runs REAL extraction on the golden set via the requested engine, so it is
    slow + costs OpenRouter credits — that's acceptable for a manual eval.
    Returns aggregate metrics (field_accuracy, item_recall, per_field, cost…).
    """
    try:
        from v11.learn import evaluate
        return {"metrics": evaluate.score_against_golden(engine=engine, limit=limit)}
    except Exception as e:
        return {"metrics": {}, "error": str(e)}


@router.get("/evaluate/scores")
async def evaluate_scores(current_user: dict = Depends(require_admin)):
    """Archived eval scores + the current best (by field_accuracy)."""
    try:
        from v11.learn import evaluate
        return {"scores": evaluate.list_scores(), "best": evaluate.best_score()}
    except Exception as e:
        return {"scores": [], "best": None, "error": str(e)}


@router.post("/evaluate/promote")
async def evaluate_promote(req: PromoteRequest,
                           current_user: dict = Depends(require_admin)):
    """Promote a candidate score iff it beats the baseline (else best-on-file)."""
    from v11.learn import evaluate
    try:
        return evaluate.promote_if_better(req.label, req.metrics, req.baseline)
    except Exception as e:
        raise HTTPException(500, f"Failed to promote: {e}")


@router.get("/proposals/llm")
async def get_llm_proposals(limit: int = 200,
                            current_user: dict = Depends(require_admin)):
    """LLM meta-agent rule proposals from review signals.

    Returns [] unless the LEARN_PROPOSER env flag is enabled (fail-safe) — the
    proposer makes an LLM call, so it stays off by default.
    """
    try:
        from v11.learn import proposer
        return {"proposals": proposer.propose_rules(limit=limit)}
    except Exception as e:
        return {"proposals": [], "error": str(e)}


@router.get("/status")
async def learn_status(current_user: dict = Depends(require_admin)):
    """Which self-improvement features are enabled + how much data has accrued."""
    import os
    from v11.learn import rules, weakspots
    from v11.learn import fewshot

    def _on(name):
        return str(os.getenv(name, "0")).strip().lower() in ("1", "true", "yes", "on")

    return {
        "flags": {
            "LEARN_FEWSHOT_PRIMARY": _on("LEARN_FEWSHOT_PRIMARY"),
            "LEARN_FEWSHOT_SHADOW": _on("LEARN_FEWSHOT_SHADOW"),
            "LEARN_PROMPT_RULES": _on("LEARN_PROMPT_RULES"),
            "LEARN_AUTO_PRIORS": _on("LEARN_AUTO_PRIORS"),
            "LEARN_ADAPTIVE_VOTES": _on("LEARN_ADAPTIVE_VOTES"),
            "LEARN_PROPOSER": _on("LEARN_PROPOSER"),
        },
        "approved_rules": len(rules.list_approved()),
        "frequently_corrected_fields": fewshot.frequently_corrected_fields(),
        "weak_fields": weakspots.weak_fields(),
    }
