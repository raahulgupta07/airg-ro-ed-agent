"""City Agent ROVER orchestrator (v1 — full-doc, 4-family).

  Tier 0 deterministic -> Tier 1 vision fleet (grok) -> deterministic supervisor
  -> Tier 2 challenger (gpt) on suspect columns only -> compiled record.

run(pdf_path, use_challenger=True) -> dict.
"""
import time
from typing import Dict

from . import context, deterministic, supervisor, llm
from .vision_agents import FAMILIES, run_family, _EVIDENCE
from .schema import Cell, COLUMNS


def _cost(usage) -> float:
    return float((usage or {}).get("cost") or 0)


def _run_challenger(ctx, suspect, primary_rec, images=None) -> Dict[str, Cell]:
    """One call to a DIFFERENT model for just the suspect columns. Agreement with
    the primary raises trust; disagreement keeps the column in review.

    Fix C — send only the routed field-bearing pages (`images`), not the whole doc.
    On a 28-page scan, all-pages made the challenger cost ~$0.4; the fields live on
    the same 1–2 pages the primary already read."""
    rules = []
    for fam in FAMILIES.values():
        for col in fam["columns"]:
            if col in suspect:
                rules.append(fam["prompt"])
                break
    prompt = ("Re-read this Myanmar CUSDEC. Extract ONLY these fields: "
              + ", ".join(suspect) + ".\n" + "\n".join(dict.fromkeys(rules))
              + "\n\n" + _EVIDENCE)
    parsed, raw, usage, err = llm.call(
        llm.CHALLENGER, prompt, images if images is not None else ctx.image_content())
    out = {"_usage": usage}
    for col in suspect:
        c = Cell(column=col, model=llm.CHALLENGER)
        obj = (parsed or {}).get(col) if isinstance(parsed, dict) else None
        if isinstance(obj, dict):
            c.value = obj.get("value")
            c.source = str(obj.get("source") or "")[:160]
            c.confidence = float(obj.get("confidence") or 0)
        out[col] = c
    return out


def _agree(a, b) -> bool:
    na, nb = supervisor._num(a), supervisor._num(b)
    if na is not None and nb is not None:
        return abs(na - nb) < 0.01 * max(abs(nb), 1) + 0.5
    return str(a).strip().upper() == str(b).strip().upper()


def run(pdf_path: str, use_challenger: bool = True) -> dict:
    t0 = time.time()
    ctx = context.load(pdf_path)
    cost = 0.0

    # Tier 0
    det = deterministic.extract(ctx.all_lines)

    # Tier 1 — vision fleet (sequential; families are independent, could be threaded)
    vis: Dict[str, Cell] = {}
    for fam in FAMILIES:
        res = run_family(ctx, fam)
        cost += _cost(res.pop("_usage", {}))
        res.pop("_err", None)
        vis.update(res)

    # Supervisor
    rec, suspect, notes, needs_review = supervisor.compile(det, vis)

    # Tier 2 — challenger on suspect columns only
    challenger_report = {}
    if use_challenger and suspect:
        ch = _run_challenger(ctx, suspect, rec)
        cost += _cost(ch.pop("_usage", {}))
        for col in suspect:
            prim = rec.get(col)
            chc = ch.get(col)
            if not chc or chc.value in (None, ""):
                challenger_report[col] = "challenger-empty"
                continue
            if _agree(prim.value, chc.value):
                # models agree -> trust the value, clear the flag
                rec[col].status = "ok"
                rec[col].note += " | challenger-agrees"
                challenger_report[col] = f"agree ({chc.value})"
            else:
                rec[col].status = "review"
                rec[col].note += f" | challenger-DISAGREES ({chc.value})"
                rec[col].alternates.append(chc.value)
                challenger_report[col] = f"DISAGREE prim={prim.value} chal={chc.value}"
        needs_review = any(c.status in ("suspect", "review")
                           for c in rec.values() if isinstance(c, Cell))

    return {
        "pdf": pdf_path.split("/")[-1],
        "sec": round(time.time() - t0, 1),
        "cost": round(cost, 4),
        "needs_review": needs_review,
        "suspect": suspect,
        "notes": notes,
        "challenger": challenger_report,
        "record": {c: rec[c].as_dict() for c in COLUMNS if c in rec},
        "values": {c: (rec[c].value if c in rec else None) for c in COLUMNS},
    }
