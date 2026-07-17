"""City Agent ROVER — Release Order Verification & Extraction Reader.

A per-column, evidence-first, math-verified extraction engine for Myanmar customs
Release Orders. Runs beside the live Atlas V14 pipeline (v11/, v13/). Design:

  Tier 0  deterministic (no LLM)  — text-layer fields on digital pages
                                    (declaration_no w/ First-approval rule, dates)
  Tier 1  primary vision fleet    — family column-agents on ONE shared model (grok)
                                    each returns {value, source, confidence}
  Supervisor = deterministic math — reconcile-style invariants, merge, flag suspect
  Tier 2  challenger (diff model) — fires ONLY on suspect/low-confidence columns

Nothing here touches the live pipeline; run via fleet.pipeline.run(pdf_path).
"""
