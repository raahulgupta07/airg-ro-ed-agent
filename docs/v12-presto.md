# V12 "Presto" — typed fast-path

> Status: **built, flag OFF.** Safe to deploy as-is (no behavior change until
> `PRESTO_ENABLED=1`). Branch: `feature/v12-presto`.

## What it is
A fast lane for **typed, digital** customs PDFs (machine-printed MACCS with a
text layer). Instead of rendering 8×300 DPI images and making ~20 vision/LLM
calls, Presto reads the **exact text already in the PDF** (`fitz.get_text`) and
makes **one** schema-constrained LLM call to structure it.

It is an addition to **V11 Maestro**, not a rewrite. V11 still classifies and
routes; Presto is just a faster engine for the typed branch. Handwritten
(V10 PRO / Scrivener) and scanned-typed (V7 / Veritas) paths are untouched.

## Why
Measured on a real 10-page MACCS declaration:

| | V7 (Veritas) | **Presto** |
|---|---|---|
| Time | ~80–86 s | **~20 s** (4× faster) |
| Cost | ~$0.10–0.16 | **~$0.013** (8× cheaper) |
| Items | 16 (sometimes needs recovery) | **16 in one call** |
| Reconcile | sometimes needs re-run | **balanced first try** |
| LLM calls | ~20 | **1** |

The typed path was reading text from pixels that was already in the file.

## How it works
```
classify (+ has_text_layer probe)
  └─ TYPED + has_text_layer + PRESTO_ENABLED → Presto
         fitz.get_text(words) → 1 schema call → declaration + items + bboxes
         → reconcile (Σ items == declared total?)
              balanced → keep Presto result
              NOT balanced / error → fall back to V7 Veritas
  └─ TYPED + scanned (no text layer) → V7 (unchanged)
  └─ HANDWRITTEN → V10 PRO (unchanged)
```

**Accuracy guarantee:** Presto's result is only kept if the arithmetic closure
balances; otherwise it silently falls back to V7. It cannot ship a worse result
than V7 — worst case it adds a few seconds then runs V7 anyway.

## Files
| File | Role |
|---|---|
| `backend/v11/config.py` | `PRESTO_ENABLED`, `PRESTO_TEXT_LAYER_MIN_CHARS` |
| `backend/v11/agents/page_classifier.py` | per-page `has_text_layer` probe |
| `backend/v11/presto_schema.py` | strict Pydantic output schema |
| `backend/v11/presto.py` | the extractor (`run(pdf_path)`) |
| `backend/v11/workflow.py` | `_call_typed()` routing + math-gate |
| `backend/scripts/presto_shadow.py` | offline V7-vs-Presto comparison |

## Config (env)
| Var | Default | Meaning |
|---|---|---|
| `PRESTO_ENABLED` | `0` (off) | turn the fast-path on |
| `PRESTO_TEXT_LAYER_MIN_CHARS` | `120` | min chars/page to call a page "digital" |

## Rollout — do these in order

### 1. Deploy (flag off, zero risk)
```bash
git pull origin main          # after the branch is merged
docker compose build app worker
docker compose up -d
```
Nothing changes — V11 behaves exactly as before.

### 2. Prove on real PDFs (the go/no-go gate)
Put 20–50 real customs PDFs somewhere the container can read, then:
```bash
docker compose exec app python -m scripts.presto_shadow /app/data/uploads --json /app/data/presto_report.json
```
Read the **AGGREGATE** block. Enable only if:
- item-count match is high,
- "decl fully matched" is high,
- Presto balances **≥** V7.

Any doc flagged `CHK` lists the exact field differences — review them.

### 3. Enable for typed
```bash
# .env
PRESTO_ENABLED=1
```
```bash
docker compose up -d
```
Typed digital docs now use Presto; scanned/handwritten unchanged. The `trace`
shows `{"phase":"route_typed","engine":"presto"}` and each typed result carries
`_engine: "presto"` or `"v7"`.

### 4. Rollback (instant)
```bash
# .env
PRESTO_ENABLED=0
```
```bash
docker compose up -d
```

## Not done yet (Phase 6, optional/later)
- **Scanned typed** (no text layer) → RapidOCR/PaddleOCR CPU pre-pass → Presto.
- **Handwriting** → A/B Google Document AI HTR vs current multi-DPI vote
  (check customs data-egress compliance first).
