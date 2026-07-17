"""Bounded recovery agent — the PROPOSER, never the judge. Fires only on the
columns the deterministic supervisor already flagged for review. For each suspect
field it runs recovery tools in a fixed order (today: a high-DPI zoom re-read) and,
after every attempt, RE-RUNS `supervisor.check` on the record. A field leaves the
suspect set ONLY when the arithmetic/rules stop flagging it — the agent proposes a
value with evidence, the supervisor decides. A zoom read that doesn't satisfy the
invariants is reverted and kept as a human-facing alternate, never silently trusted.
Budget-limited so cost stays bounded; every tool call is guarded so a failure can
never raise out of `recover`. OpenRouter-only (zoom owns the LLM; nothing here)."""
from . import supervisor

# suspect column -> (label to locate on the page, field_name to ask the model for).
# Columns with no mapping are skipped (no zoom tool exists for them).
_LABELS = {
    "exchange_rate": ("Exchange Rate", "exchange rate"),
    "import_export_customs_duty": ("IMPORT/EXPORT CUSTOMS DUTY", "customs duty amount"),
    "total_customs_value": ("Total customs value", "total customs value"),
    "commercial_tax_ct": ("COMMERCIAL TAX", "commercial tax amount"),
}


def recover(ctx, rec, suspect, budget=5, model=None) -> dict:
    """Try to recover the flagged columns in `suspect` (a list of column names) on
    record `rec` (dict of column -> Cell). For each mapped, still-flagged column,
    spend one unit of budget on a zoom re-read; tentatively install the zoom value,
    re-run the deterministic supervisor, and KEEP it only if the math now accepts the
    column. Otherwise REVERT exactly and stash the zoom value as an alternate.

    Returns {"rec", "suspect", "needs_review", "report"} where `report` carries the
    per-attempt trace and the token usage of every tool call. Fail-safe: any tool
    exception is swallowed and recorded as a failed attempt — never propagated."""
    report = {"attempts": [], "recovered": [], "cost_usage": []}
    spent = 0

    for col in list(suspect):
        if spent >= budget:
            break
        if col not in _LABELS:
            continue
        label, field_name = _LABELS[col]

        cell = rec.get(col)
        if cell is None:                       # only cells actually in the record
            continue

        # Snapshot BEFORE any mutation so a revert restores the cell exactly.
        snap = (cell.value, cell.model, cell.status, cell.confidence, cell.note)

        try:
            from . import zoom
            res = zoom.zoom_field(ctx, label, field_name, model)
        except Exception as e:  # noqa: BLE001 — a tool failure must never escape
            report["attempts"].append((col, "error", str(e)))
            continue
        spent += 1
        report["cost_usage"].append(res.get("usage", {}))

        val = res.get("value")
        conf = res.get("confidence", 0) or 0
        if val is None or conf < 0.5:
            report["attempts"].append((col, "no-read"))
            continue

        # Tentatively PROPOSE the zoom value; keep the prior value as an alternate.
        try:
            if snap[0] not in (None, "") and snap[0] not in cell.alternates:
                cell.alternates.append(snap[0])
            cell.value = val
            cell.model = "zoom"
            cell.confidence = conf
            cell.status = "ok"
            cell.note = (cell.note + "; " if cell.note else "") + "zoom recovery"

            new_suspect, _ = supervisor.check(rec)
        except Exception as e:  # noqa: BLE001 — restore and record, never raise
            cell.value, cell.model, cell.status, cell.confidence, cell.note = snap
            report["attempts"].append((col, "error", str(e)))
            continue

        if col not in new_suspect:
            # The deterministic supervisor now accepts it — proposal stands.
            report["recovered"].append(col)
            report["attempts"].append((col, "recovered", val))
        else:
            # Invariants still reject it — don't trust the zoom read. Revert exactly,
            # but leave the candidate as an alternate for the human reviewer.
            cell.value, cell.model, cell.status, cell.confidence, cell.note = snap
            if val not in cell.alternates:
                cell.alternates.append(val)
            report["attempts"].append((col, "rejected", val))

    # Final verdict is the supervisor's, plus any cell still parked in a review state.
    try:
        final_suspect, _ = supervisor.check(rec)
    except Exception:  # noqa: BLE001 — fail-closed if the check itself blows up
        final_suspect = list(suspect)
    for col, c in rec.items():
        if getattr(c, "status", None) in ("suspect", "review") and col not in final_suspect:
            final_suspect.append(col)

    return {
        "rec": rec,
        "suspect": final_suspect,
        "needs_review": len(final_suspect) > 0,
        "report": report,
    }
