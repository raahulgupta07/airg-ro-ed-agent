# City Agent ROVER — Verified Release-Order Extraction Engine

**Name:** City Agent **ROVER** = **R**elease **O**rder **V**erification & **E**xtraction **R**eader.
Code lives in `backend/fleet/` (internal package name kept; product name is ROVER).
**Relationship to Atlas:** Atlas V14 (`v11/`, `v13/`) is the LIVE production engine. ROVER is
a new, side-by-side extraction engine — evidence-first, math-verified, human-in-the-loop.
Not yet flag-gated into the live app.

**Guarantee:** *Every number that ships is correct* — each field is either read directly and
accepted by the math checks, or it is flagged for a one-click human confirm. Nothing uncertain
ships silently. Output accuracy = 100%; autonomy ≈ 85% (the rest need a human confirm).

---

## 1. How it works (the process)

```
PDF
 ① Tier 0 deterministic ($0)   context.py + deterministic.py
     text layer → declaration_no (First-approval) + declaration_no_official + date + printed rate
     OCR-fix + validate 11–12 digits
 ② L1 page router              router.py     → send only the 1–2 field-bearing pages
 ③ Tier 1 vision (grok-4.5)    single_agent.py → all header fields + evidence + confidence, 1 call
 ④ Product lane                products.py + item_text.py → find ALL item pages, every product,
                                dedup bundle repeats, recover quantities (guarded)
 ⑤ Supervisor — math judge ($0) supervisor.py
     merge · derive rate=MMK/USD (advisory→flag) · decl_no cross-check (text vs vision)
     invariants: rate-band · core-tax · CT≠exemption · confidence · product count vs declared
 ⑥ Recovery agent              recovery.py + zoom.py → cell-zoom (400 DPI crop) of suspect fields;
                                supervisor RE-JUDGES (clears only if math accepts)
 ⑦ Challenger (gpt-5.6-luna)   pipeline.py → 2nd model on remaining suspects; agree→clear, else review
 ⑧ V1 rescue                   full doc, all pages; clean+direct reads → adopt, else → human
 ⑨ Human review               review.py → flagged fields + evidence → confirm → back to store
 ⑩ Mapping                     mapping.py → accountant "AI results" schema (both decl nos, both values,
                                TOTAL IMPORT, SF≠MACCS, review flag, notes)
 ⑪ Store                       store.py → persist as DATA (documents + line items) + report definitions
 ⑫ Annotate                    annotate.py → box every field on the form, colored by status
```

**Rules:** direct reads auto-pass · derivations always flag · math decides · every field carries
evidence (quote + confidence + model) · nothing ships unproven.

**Models (OpenRouter only):** `x-ai/grok-4.5` (primary: read/product/zoom/rescue) ·
`openai/gpt-5.6-luna-pro` (challenger). Config in `llm.py`. No LLM in ①⑤⑩⑪⑫.

---

## 2. Data storage — DECISION

**Primary store = PostgreSQL (JSONB).** The app already runs PG — reuse it, no new infra.
**DuckDB = optional analytics layer LATER** (heavy cross-doc aggregation / "chat with extractions"),
never the system of record (embedded, single-writer).

Two grains = two tables; reports are saved queries (not copies of data), so new docs auto-appear.

```sql
CREATE TABLE custos_documents (
  doc_id       text PRIMARY KEY,        -- declaration_no or pdf stem
  pdf          text,
  header       jsonb,      -- 15 fields, each cell = {value,source,confidence,model,status,alternates}
  needs_review boolean,
  suspect      jsonb,
  reviewed     boolean DEFAULT false,
  cost         numeric,
  created_at   timestamptz DEFAULT now()
);
CREATE TABLE custos_items (
  id bigserial PRIMARY KEY,
  doc_id text REFERENCES custos_documents(doc_id) ON DELETE CASCADE,
  no int, hs_code text, description text, quantity numeric, unit text, value numeric
);
CREATE TABLE custos_reports (
  name text PRIMARY KEY, grain text, columns jsonb, filters jsonb, sort text
);
```
- overall report = `custos_documents`; product = `custos_items`; joined = items ⋈ documents.
- Evidence lives in JSONB → annotator + review queue read it back.
- `store.py` already abstracts save/load/rows/reports → swap file backend for PG with no caller change.
- Switch via env `CUSTOS_STORE=pg|files` (files = dev/MVP default).

---

## 3. Status

### Done (this session)
- Header extraction: **91% vs accountant sheet, ~100% vs document**, evidence-backed
- declaration_no cross-check (silent-wrong caught) · both decl numbers · both value figures
- Exchange-rate: printed-read + L2 derive (advisory→flag) · fixed the Atlas overwrite bug too
- Product lane: multi-page completeness (2→7) + dedup + count cross-check (guarded)
- Recovery agent + cell-zoom · challenger · v1 rescue
- Mapping layer → accountant schema · Store (JSON files) + report definitions
- Annotate (auto evidence boxes) · Review queue (queue/apply/export/stats)
- **97 tests pass** (88 fleet+pipeline, 9 store/item_text)
- Deployed to container for runs; 3 explainer/tool artifacts published

### Pending → production
| P | item |
|---|------|
| P1 | **Postgres store** — migration for the 3 tables + `store_pg.py` (same API) + `CUSTOS_STORE` flag |
| P2 | **Flag-gate** Custos behind env (run beside Atlas, OFF default) + env-configurable models |
| P3 | **Commit + version bump** (branch `custos-engine` off dev; never straight to main) |
| P4 | **Review UI** — screen over `review.py` (CSV export exists); annotated preview inline |
| P5 | **Quantity on scans** — pass explicit region to zoom for scanned item tables |
| P6 | **declared_count parser** — robust "Total items" read (currently guarded/ignored) |
| P7 | **A/B** Custos vs Atlas, per-field, 14 docs → decide replace/augment |
| P8 | **README** for `backend/fleet/` |
| P9 | **Bake the 5 accountant answers** into mapping (91% → ~100% vs sheet) |
| P10 | **TOON adapter** — only when an LLM-over-stored-data feature exists (~40% token cut there) |

### Known limits (honest)
- Handwritten docs (MA0259) → human review; no autonomous read (physical limit).
- Quantities on scanned scrambled item pages → honest null (not guessed) until P5.
- "91% vs sheet" gap = accountant re-booked figures, not extraction error (fixed by P9 mapping).

---

## 4. Costs
- clean digital (~85%): **$0.013–0.016/doc**
- hard scan (recovery/rescue): $0.10–0.50/doc
- handwriting: → human
- primary read is grok-4.5; challenger/rescue fire only on flagged docs.
