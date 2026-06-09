"""V11 — "JUDGE" confidence-routing.

Every extraction (V7 / Presto / Scribe) funnels through a merge→save boundary.
Some results are clean enough to auto-approve; some need a human. JUDGE makes
that call from signals that ALREADY EXIST on the result — it calls NO LLM, opens
NO PDF, makes NO network request. It is a cheap, deterministic arbiter that turns
a pile of existing evidence into one number and one boolean.

Inputs it reads (all optional, all best-effort):
  * the reconcile() verdict (item-sum closure + CIF closure) — the arithmetic
    backbone. See v11/tools/reconcile.py for the dict shape.
  * result["scribe"]["field_confidence"]   (dict field→0..1, self-consistency)
  * result["scribe"]["low_confidence_fields"]  (list of weak fields)
  * result.get("_engine")  ("presto" | "presto+selfcorrect" | "v7" | ...)
    — "selfcorrect" present means the FIXER had to fire, a mild trust penalty.

Output:
  {
    "auto_ok": bool,        # safe to auto-approve
    "score": float,         # 0..1, rounded 3dp
    "threshold": float,     # the bar score had to clear
    "reasons": [str, ...],  # short human-readable score drivers
    "needs_review": bool,   # == not auto_ok
  }

auto_ok is gated: it is True only when score >= threshold AND the reconcile
verdict is both `checked` and `balanced`. A high score can never override broken
arithmetic. JUDGE never raises — on any internal error it returns a conservative
"send to human" verdict.
"""
import os
from typing import Dict, List, Optional


# --- scoring weights (sum of positive caps ~= 1.0) -----------------------------
# These are intentionally simple and documented so the score is auditable.
# Weights are engine-agnostic: the math GATES carry the score so a clean typed
# (Presto, no per-field votes) doc can auto-pass. Scribe's field_confidence is a
# BONUS on top, not a requirement — absence must not block auto-pass.
_W_GATES = 0.55        # reconcile checked+balanced — the dominant signal
_W_CIF = 0.15          # CIF closure (exchange-rate sanity)
_W_ITEMS = 0.10        # at least one line item was extracted
_W_DUTY = 0.10         # duty closure ok (advisory invariant)
_W_FIELD_CONF = 0.10   # scribe field-confidence avg — bonus only (0 when absent)

# penalties (subtracted, clamped at the end)
_P_LOWCONF_EACH = 0.06     # per low-confidence field
_P_LOWCONF_MAX = 0.30      # cap on the low-conf penalty
_P_SELFCORRECT = 0.05      # FIXER had to fire
_P_GAP_SCALE = 0.50        # fraction of (gap_pct/100) charged against score
_P_GAP_MAX = 0.30          # cap on item-sum gap penalty
_P_CIF_GAP_SCALE = 0.30    # fraction of (cif_gap_pct/100) charged
_P_CIF_GAP_MAX = 0.15      # cap on cif gap penalty


def _default_threshold() -> float:
    """Auto-approve bar. Env override, else 0.8."""
    try:
        return float(os.environ.get("JUDGE_AUTO_THRESHOLD", "0.8"))
    except Exception:
        return 0.8


def _field_conf_avg(result: Dict) -> Optional[float]:
    """Average of scribe per-field confidences, or None if not present."""
    try:
        scribe = result.get("scribe") or {}
        fc = scribe.get("field_confidence") or {}
        vals = [float(v) for v in fc.values()
                if isinstance(v, (int, float))]
        if not vals:
            return None
        return sum(vals) / len(vals)
    except Exception:
        return None


def _low_conf_count(result: Dict) -> int:
    try:
        scribe = result.get("scribe") or {}
        lc = scribe.get("low_confidence_fields") or []
        return len(lc)
    except Exception:
        return 0


def judge(result: dict, verdict: dict = None, *,
          threshold: float = None) -> dict:
    """Score an extraction result and decide auto-approve vs human-review.

    Args:
        result:   an extraction result dict (V7 / Presto / Scribe shaped).
        verdict:  a reconcile() verdict dict. If None, it is derived from
                  result["declaration"] and result["items"].
        threshold: auto-approve bar. Overrides JUDGE_AUTO_THRESHOLD env / 0.8.

    Returns:
        {"auto_ok", "score", "threshold", "reasons", "needs_review"}.
        Never raises.
    """
    thr = threshold if threshold is not None else _default_threshold()
    try:
        # 1. Get a reconcile verdict (derive if not handed one).
        if verdict is None:
            from v11.tools import reconcile
            verdict = reconcile.reconcile(
                result.get("declaration") or {},
                result.get("items") or [],
            )
        verdict = verdict or {}

        checked = bool(verdict.get("checked"))
        balanced = bool(verdict.get("balanced"))
        cif_checked = bool(verdict.get("cif_checked"))
        cif_ok = bool(verdict.get("cif_ok"))
        gap_pct = float(verdict.get("gap_pct") or 0.0)
        cif_gap_pct = float(verdict.get("cif_gap_pct") or 0.0)
        n_items = int(verdict.get("n_items") or 0)

        reasons: List[str] = []
        score = 0.0

        # 2. POSITIVE signals --------------------------------------------------
        # The arithmetic gate is the dominant trust signal.
        if checked and balanced:
            score += _W_GATES
            reasons.append("reconcile balanced (+%.2f)" % _W_GATES)
        elif checked and not balanced:
            reasons.append("reconcile checked but NOT balanced (gap %.2f%%)"
                           % gap_pct)
        else:
            reasons.append("reconcile could not check (no anchor)")

        # CIF closure (exchange-rate sanity).
        if cif_checked and cif_ok:
            score += _W_CIF
            reasons.append("CIF closure ok (+%.2f)" % _W_CIF)
        elif cif_checked and not cif_ok:
            reasons.append("CIF closure FAILED (gap %.2f%%)" % cif_gap_pct)

        # At least one line item present.
        if n_items > 0:
            score += _W_ITEMS
            reasons.append("%d item(s) (+%.2f)" % (n_items, _W_ITEMS))
        else:
            reasons.append("no items extracted")

        # Duty closure (advisory): duty ≈ Σ(item value × rate).
        if verdict.get("duty_checked"):
            if verdict.get("duty_ok"):
                score += _W_DUTY
                reasons.append("duty closure ok (+%.2f)" % _W_DUTY)
            else:
                reasons.append("duty closure off (gap %.2f%%)" % (verdict.get("duty_gap_pct") or 0))

        # Per-row math: a suspect item value tanks the score (forces review).
        if verdict.get("rows_checked") and not verdict.get("rows_ok"):
            nbad = len(verdict.get("bad_rows") or [])
            score -= 0.30
            reasons.append("%d item row(s) fail value=qty×price×rate (-0.30)" % nbad)

        # Scribe field-confidence average, scaled by its own magnitude.
        fc_avg = _field_conf_avg(result)
        if fc_avg is not None:
            contrib = _W_FIELD_CONF * max(0.0, min(1.0, fc_avg))
            score += contrib
            reasons.append("field_confidence avg %.2f (+%.2f)"
                           % (fc_avg, contrib))

        # 3. NEGATIVE signals --------------------------------------------------
        # Low-confidence fields (scribe self-consistency disagreements).
        lc = _low_conf_count(result)
        if lc > 0:
            pen = min(_P_LOWCONF_MAX, lc * _P_LOWCONF_EACH)
            score -= pen
            reasons.append("%d low-confidence field(s) (-%.2f)" % (lc, pen))

        # FIXER had to fire (self-correction engine).
        engine = str(result.get("_engine") or "")
        if "selfcorrect" in engine.lower():
            score -= _P_SELFCORRECT
            reasons.append("self-correct fired (-%.2f)" % _P_SELFCORRECT)

        # Item-sum gap magnitude (graded penalty, even within tolerance).
        if gap_pct > 0:
            # pen = clamp(gap_pct/100 * scale, max)
            pen = min(_P_GAP_MAX, (gap_pct / 100.0) * _P_GAP_SCALE)
            if pen > 0:
                score -= pen
                reasons.append("item-sum gap %.2f%% (-%.2f)" % (gap_pct, pen))

        # CIF gap magnitude.
        if cif_gap_pct > 0:
            pen = min(_P_CIF_GAP_MAX, (cif_gap_pct / 100.0) * _P_CIF_GAP_SCALE)
            if pen > 0:
                score -= pen
                reasons.append("cif gap %.2f%% (-%.2f)" % (cif_gap_pct, pen))

        # 4. Clamp + decide ----------------------------------------------------
        score = max(0.0, min(1.0, score))
        score = round(score, 3)

        auto_ok = bool(score >= thr and balanced and checked)
        if not auto_ok and score >= thr and not (balanced and checked):
            reasons.append("score cleared bar but arithmetic gate not passed "
                           "→ review")

        return {
            "auto_ok": auto_ok,
            "score": score,
            "threshold": round(float(thr), 3),
            "reasons": reasons,
            "needs_review": not auto_ok,
        }
    except Exception as e:
        # Conservative fallback: when in doubt, a human looks at it.
        return {
            "auto_ok": False,
            "score": 0.0,
            "threshold": round(float(thr), 3),
            "reasons": ["judge error: %s" % e],
            "needs_review": True,
        }


# CLI: python backend/v11/tools/judge.py  — smoke test, no PDF/API needed.
if __name__ == "__main__":
    import json

    # A clean, balanced result with strong field confidence.
    clean = {
        "_engine": "presto",
        "declaration": {
            "total_customs_value": 10000000,
            "invoice_price": 5000,
            "exchange_rate": 2000,
        },
        "items": [
            {"item_name": "WIDGET", "hs_code": "1234.56",
             "customs_value_mmk": 6000000},
            {"item_name": "GADGET", "hs_code": "6543.21",
             "customs_value_mmk": 4000000},
        ],
        "scribe": {
            "field_confidence": {"total_customs_value": 1.0,
                                 "importer_name": 1.0,
                                 "exchange_rate": 0.95},
            "low_confidence_fields": [],
        },
    }

    # An unbalanced result: items don't sum to the declared total, the FIXER
    # fired, and several fields were read with low confidence.
    messy = {
        "_engine": "presto+selfcorrect",
        "declaration": {
            "total_customs_value": 10000000,
            "invoice_price": 5000,
            "exchange_rate": 50,   # wrong rate → CIF closure fails
        },
        "items": [
            {"item_name": "WIDGET?", "hs_code": "1234.56",
             "customs_value_mmk": 3000000},
        ],
        "scribe": {
            "field_confidence": {"total_customs_value": 0.4,
                                 "importer_name": 0.5,
                                 "exchange_rate": 0.3},
            "low_confidence_fields": ["total_customs_value", "importer_name",
                                      "exchange_rate"],
        },
    }

    for label, res in (("CLEAN", clean), ("MESSY", messy)):
        out = judge(res)
        print("=== %s ===" % label)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        print()
