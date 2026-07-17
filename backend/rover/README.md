# City Agent ROVER — Release Order Verification & Extraction Reader

ROVER is an evidence-first, math-verified, human-in-the-loop engine for extracting
Myanmar customs **Release Orders** (CUSDEC bundles) into a structured schema. Every
column value it emits is a *Cell* — a value plus **where** it was read from and how sure
it is — so no number exists without evidence. Deterministic text-layer reads and
cross-field arithmetic own the fields they can prove; a vision fleet reads the rest; a
deterministic supervisor judges the math and flags anything suspect; a challenger model
and bounded recovery re-check only what's uncertain; and whatever the math still can't
settle is routed to a human. It runs **side-by-side with the live Atlas V14 engine** as
an independent, flag-gated alternative.

## Guarantee

Every shipped number is either **read + math-verified** (deterministic value that
satisfies the cross-field invariants) or **human-confirmed** in review. Nothing uncertain
ships silently — a field that fails its checks is marked `needs_review` and held for a
person. The result is **verified 100%**: confidence comes from evidence and arithmetic,
never from a model's say-so.

## Pipeline

1. **deterministic** (`context` → `deterministic`) — render page images + read the text
   layer; extract the fields the text layer proves ($0, no LLM).
2. **router** — send only the Release-Order pages that carry scored fields to vision
   (~70% fewer image tokens).
3. **single_agent** (grok) — one primary vision call returns every column with per-column
   evidence.
4. **products + item_text** — item lane: find every goods page and extract every line item
   (vision), with a deterministic text-layer parser for scrambled quantities.
5. **supervisor** (math judge) — deterministic COMPILER + JUDGE: merge cells, run
   cross-field invariants, mark suspect columns; fail-closed.
6. **recovery + zoom** — bounded PROPOSER: for each suspect field, re-read a high-DPI crop
   and re-run the supervisor check.
7. **challenger** (gpt) — second-opinion model on suspect columns only.
8. **v1 rescue** (`pipeline.py`) — full-doc 4-family fallback path for the fast pipeline.
9. **review** — turn remaining flagged fields + evidence into a human decision list.
10. **mapping** — rename/derive/normalize the fleet record into the accountant's Excel
    schema.
11. **store** — persist each extraction once as durable JSON (or Postgres) data.
12. **annotate** — draw colored evidence boxes on the source PDF over each read value.

## Modules

| File | Role |
|---|---|
| `context.py` | Shared read-only blackboard: rendered page images + text layer. |
| `deterministic.py` | Tier 0 text-layer header extraction (declaration no/date, etc.), no LLM. |
| `router.py` | L1 page router — sends only field-bearing pages to vision. |
| `single_agent.py` | L2 single vision call returning every column with evidence. |
| `vision_agents.py` | Tier 1 primary vision fleet — family column-agents on one shared model. |
| `products.py` | Item lane — find every item page, extract every goods row, don't drop a product. |
| `item_text.py` | Tier 0 deterministic text-layer parser for item quantities, no LLM. |
| `supervisor.py` | Deterministic COMPILER + JUDGE — merge, run math invariants, flag suspects. |
| `recovery.py` | Bounded PROPOSER on suspect fields; re-runs supervisor after each attempt. |
| `zoom.py` | Zoom-read tool — recover one field from a high-DPI crop. |
| `pipeline.py` | v1 orchestrator — full-doc 4-family fleet (also the fast path's rescue). |
| `pipeline_fast.py` | v2 orchestrator — L1 route + L2 single call, same tiers, no image waste. |
| `mapping.py` | Fleet record → accountant's Excel schema (rename/derive/normalize). |
| `store.py` | Persist extractions as JSON data + saved report definitions; backend-switchable. |
| `store_pg.py` | Postgres-backed store (bound in when `ROVER_STORE=pg`; falls back to files). |
| `annotate.py` | Draw colored evidence boxes on the PDF over each field's read location. |
| `review.py` | Human-in-the-loop layer — flagged fields + evidence → verified 100%. |
| `llm.py` | OpenRouter call (OpenRouter-only) shared by the vision fleet + challenger. |
| `schema.py` | Evidence contract — the `Cell` (value + source + confidence); no source ⇒ rejected. |

## Run it

```bash
python -m rover.run_fast <doc_id> [--no-challenger]   # single doc, fast (v2) pipeline
python -m rover.run_batch                             # all docs in /app/data/_uat_test
python -m rover.run_one <doc_id>                      # single doc, v1 full pipeline
```

## Config (env)

| Var | Meaning |
|---|---|
| `ROVER_PRIMARY_MODEL` | Primary read/product/zoom/rescue model (default `x-ai/grok-4.5`). |
| `ROVER_CHALLENGER_MODEL` | Challenger second-opinion model (default `openai/gpt-5.6-luna-pro`). |
| `ROVER_STORE` | Storage backend: `files` (default) or `pg`. |
| `ROVER_STORE_DIR` | Directory for the files backend (default `/app/data/rover_store`). |

All model calls go through **OpenRouter** using `config.API_KEY` — never a direct vendor SDK.

## HTTP API

Mounted at **`/api/rover`** (all endpoints require auth):

| Method | Path | Purpose |
|---|---|---|
| POST | `/extract` | Run extraction on a document. |
| GET | `/documents` | List stored documents. |
| GET | `/documents/{id}` | Get one document's extraction. |
| GET | `/products` | List product/line-item rows across documents. |
| GET | `/review` | List fields flagged for human review. |
| POST | `/review/{id}/apply` | Apply a reviewer's decisions to a document. |
| GET | `/stats` | Summary counts / stats. |
| GET | `/export.csv` | Export the mapped rows as CSV. |
| GET | `/annotate/{id}` | Evidence-annotated PDF for a document. |

## Tests

```bash
pytest tests/test_rover.py tests/test_rover_store.py
```

Part of the project's 97-test suite.

## Models

- **`grok-4.5`** (`ROVER_PRIMARY_MODEL`) — primary read, product lane, zoom recovery, and
  v1 rescue.
- **`gpt-5.6-luna-pro`** (`ROVER_CHALLENGER_MODEL`) — challenger, second opinion on suspect
  columns only.
- The **deterministic** (text-layer) and **math** (supervisor/reconcile) steps use **no
  LLM** — they are pure and free.

## Status

ROVER runs **side-by-side with the live Atlas V14 engine**, is **flag-gated OFF by
default**, and is **not yet the live default**.
