# CLAUDE.md — Project Guide for AI Assistants

## What This Project Is

**RO-ED AI Agent** — Myanmar customs PDF extraction system. Routes each page of a customs declaration to the right specialist (typed pages → Veritas, handwritten pages → Scrivener), extracts structured data, and presents it for human review and approval. Built by City AI Team — City Holdings Myanmar.

The active pipeline is **V11 Maestro**. It is queue-driven, streams live router events over SSE, and writes to Postgres.

## Tech Stack

- **Frontend:** SvelteKit 5 (runes) + TailwindCSS 4.2 + ECharts
- **Backend:** FastAPI 0.115 + Uvicorn (Python 3.12, 2 workers)
- **Database:** **Postgres 16** (psycopg3 + SQLAlchemy QueuePool 10+10)
- **Job queue:** **RQ + Redis** (`worker` service, replicas: 2)
- **AI Models:** OpenRouter API (per-step model config)
  - Typed-page vision + assembler: Google Gemini 3 Flash Preview
  - Handwritten (V10 PRO): vision + arbiter + consensus models per step
  - Verifier: Anthropic Claude Sonnet 4.6
  - Fee Verifier: Gemini 3 Flash (text-based)
- **Auth:** Local JWT (HS256) + multi-LDAP cascade + Keycloak OIDC (RS256/PKCE)
- **Storage:** S3-compatible (AWS / MinIO / R2 / Wasabi / Backblaze) or local fallback. Fernet-encrypted secret keys in DB.
- **PDF:** PyMuPDF (fitz) at 300 DPI + Pillow. **No Tesseract.**
- **Container:** docker-compose (postgres + redis + app + worker x2)

## Pipeline Architecture (V11 Maestro)

```
PDF upload
  → POST /api/extract-v11      (returns 202 + {stream_id, job_id})
  → RQ enqueue → worker picks up
  → V11 workflow.py:
       1. PageClassifier        (per-page: PRINTED / INKED / EXTRA)
       2. pdf_split             (route pages by verdict)
       3. PARALLEL:
            - Veritas (V7 pipeline) on PRINTED pages
            - Scrivener (V10 PRO)    on INKED pages
       4. Merger                (combine declarations + items)
       5. field_bbox            (fitz.search_for → highlight rectangles)
       6. DB save → DONE
  → SSE /api/extract-v11/stream/{id} streams: JOB_START, CLASSIFY,
       ROUTE, STAGE_START, STAGE_DONE, MERGE, DB_SAVE, DONE, FAIL
```

V7 (legacy sync) is still mounted at `POST /api/extract` for external integrations. V10 PRO standalone at `POST /api/extract-v10-pro` is kept for testing. **All UI traffic uses V11.**

### Active extract endpoints

```
POST /api/extract                     V7 sync (legacy, external)
POST /api/extract-v10-pro             V10 PRO standalone (rare)
POST /api/extract-v11                 V11 Maestro queue → 202        ← MAIN
GET  /api/extract-v11/stream/{id}     SSE live router events
GET  /api/extract-v11/status/{id}     Poll RQ status
```

### Review API (`backend/routes/review.py`, ~15 endpoints)

Queue, stats, approve / reject / draft, field edits, items CRUD (add / update / delete / reorder), rerun, bulk approve.

## File Structure

```
backend/
  main.py                 FastAPI app + 3 extract endpoints + lifespan + auto-approve cron
  database.py             Postgres-backed; sqlite3-compat shim wraps psycopg3
  db_engine.py            SQLAlchemy QueuePool + dict-row factory + ?→%s translator
  schemas.py              Pydantic models (incl. tokens_in/out, model_used, review fields)
  worker.py               RQ Worker entrypoint
  jobs/
    queue.py              RQ + Redis singletons
    tasks.py              run_v11_task background entry
  v11/
    workflow.py           V11 Maestro orchestrator
    event_bus.py          Redis pub/sub for SSE
    agents/page_classifier.py, merger.py
    tools/pdf_split.py, field_bbox.py
  v10_pro/
    workflow.py           Scrivener (handwritten) — used by V11
  pipeline/               V7 — used by V11 for typed pages (Veritas)
    pipeline.py, splitter.py, vision.py, assembler.py, verifier.py,
    holistic_voter.py, solo_extractor.py, consensus_resolver.py,
    vision_arbiter.py, cell_zoom.py
  routes/
    auth.py, users.py, groups.py, jobs.py, data.py
    review.py             queue / stats / approve / reject / edits / items CRUD / rerun / bulk
    settings.py, ldap.py, storage.py, activity.py
    corrections.py, usage.py
  storage/                local + S3 factory pattern
  alembic/                versioned migrations
  scripts/migrate_sqlite_to_pg.py

frontend/src/
  routes/
    agent/                upload + V11 queue + side-by-side review
    history/              job list + detail (ReviewSplitView for V11)
    review/               queue w/ KPIs + filters + bulk approve
    review/[job_id]/      single-job side-by-side
    items/, declarations/ flat lists + Excel export
    costs/                tokens KPIs + 3-line trend (ECharts) + XLSX/CSV export
    settings/             USERS / GROUPS / AUTH / LDAP / STORAGE /
                          ACTIVITY_LOG / KEYCLOAK / AUTO_APPROVE
    login/
  lib/
    api.ts, stores/auth.svelte, pipelineConfig.ts (V11 only),
    utils/pipelineLabels.ts
    components/
      ReviewSplitView.svelte    side-by-side w/ inline edit
      ResultAccordion.svelte    legacy view (V7 fallback)
      ExcelTable.svelte         editable Excel-style table
      AgentTerminal.svelte      live SSE router stream
      Toast / KpiCard / Button / Badge / etc.

docker-compose.yml        postgres + redis + app + worker (x2)
Dockerfile                multi-stage (frontend build + backend)
```

## Database (Postgres 16)

High-level tables:

- **jobs** — adds `review_status`, `reviewed_by`, `reviewed_at`, `edits_count`, `parent_job_id`, `field_bboxes_json`, `tokens_in`, `tokens_out`, `model_used`, `processed_at`, `document_type`, `pipeline_mode`, `pdf_storage`
- **items** — adds `is_deleted`, `display_order`
- **declarations**
- **field_edits** — per-cell edit audit
- **activity_log** — v2 schema with 9 enrichment fields (IP / UA / auth_source / severity / duration / status / etc.)
- **ldap_configs** — Fernet-encrypted `bind_password`
- **storage_config** — Fernet-encrypted `secret_access_key`
- **app_settings** — `auto_approve_enabled`, `auto_approve_threshold`, etc.
- **users**, **groups**, **group_members**

## Review / Approve Workflow

- Side-by-side: PDF iframe (left) + editable form + Excel-style item table (right).
- Inline cell edit, ▲▼🗑 row actions, [+ ADD] row, page-jump 📍.
- Statuses: `pending_review` / `approved` / `rejected` / `draft`.
- Every cell edit → row in `field_edits`.
- Hourly cron auto-approves jobs above `auto_approve_threshold`.

## Activity Log v2

- 9 enrichment columns (IP, UA, auth_source, severity, duration, status, etc.)
- KPI strip + drawer + security tab + JOB events + filter bar.

## Cost Tracking

- `tokens_in` / `tokens_out` recorded per job.
- `/costs` page: dual-axis line chart (cost / docs / tokens), KPI cards, Excel/CSV export.

## Concurrency (target: 10 simultaneous users)

- Postgres SQLAlchemy QueuePool (10 base + 10 overflow, 30s timeout)
- Redis-backed RQ; `worker` service replicas: 2
- App: Uvicorn 2 workers
- API semaphore: max 16 simultaneous OpenRouter calls
- Per-job file isolation (S3 key or local path), no global state

## Key Design Principles

- **Zero hardcoded values** — no field names, currencies, tax codes in code
- **Zero calculations** — every value read from document, never computed
- **json_schema enforced** on all assembler calls (guaranteed JSON, all required fields)
- **Token optimized** — deduplicated fields; no metadata/visual/entities sent to assembler
- **Fee verification chain** — text-based LLM (primary) + 7-layer deterministic fallback + auto-revert safety net
- **Self-learning** — user corrections feed importer fee baselines + few-shot examples
- **Per-job isolation** — no shared mutable state between jobs
- **Memory cleanup** — image data freed after verifier step
- **Encrypted secrets at rest** — Fernet for LDAP bind passwords + S3 secret keys

## Don't (Deprecated / Removed)

- **Don't** reference V8, V9, V9_PRO, or V10 (non-pro) — directories deleted, endpoints removed
- **Don't** reference `/api/extract-v8`, `/api/extract-v9`, `/api/extract-v9-pro`, `/api/extract-v10` — gone
- **Don't** reference `/api/ws/batch` — WebSocket pipeline deleted; V11 uses SSE
- **Don't** reference `gemini_solo.py` / `opus_read.py` — deleted
- **Don't** reference SQLite — migrated to Postgres (the `sqlite3` shim only translates the legacy DB-API surface)
- **Don't** use Tesseract (project policy)
- **Don't** add calculations to prompts
- **Don't** hardcode field names, currencies, or patterns
- **Don't** commit `.env`
- **Don't** use `sqlite3.connect()` directly — use `database._connect()` (psycopg3 underneath)
- **Don't** use bare `except:` — always `except Exception:`
- **Don't** use `json_object` mode — use `json_schema` (9× faster)
- **Don't** use `res.json()` in `api.ts` — use `res.text()` + `JSON.parse()` (see troubleshooting)
- **Don't** reference `{@const}` outside its enclosing block in Svelte 5
- **Don't** mutate `$state` array entries in-place — replace with new object
- **Don't** convert `declaration_no` to float — string field
- **Don't** use vision/images for fee verification — text-based avoids layout confusion and is 10× cheaper

## Known Issues & Troubleshooting

### 1. SQLite → Postgres compatibility
The codebase preserved the `sqlite3`-style API surface for minimum churn. `db_engine.py` provides a dict-row factory and a `?` → `%s` placeholder translator. If a query fails on Postgres but worked on SQLite, check: `?` placeholders, `INTEGER PRIMARY KEY AUTOINCREMENT` (use `SERIAL`), `INSERT OR REPLACE` (use `ON CONFLICT`), and date functions.

### 2. `res.json()` hangs in browser (fetch returns 200 but body never arrives)
**Symptom:** Page shows "LOADING..." forever. Server logs show 200 OK. curl works fine.
**Root cause:** Starlette SPA middleware streams API responses; some browsers (confirmed Brave) hang on `res.json()`.
**Fix:** In `api.ts`, always use `res.text()` + `JSON.parse()`.

### 3. Svelte 5 `$derived` doesn't re-render after `$state` array mutation
**Symptom:** Queue shows DONE but pipeline view stays.
**Fix:** Replace the array entry with a new object: `queue[i] = { ...entry, status: 'done' }; queue = [...queue];`

### 4. `{@const}` scoping
`{@const}` is scoped to its enclosing `{#if}` / `{#each}` / `{#snippet}`. Move it inside, inline the expression, or hoist to a `$derived` at the script level.

### 5. FastAPI trailing-slash redirects (307)
Browser strips `Authorization` on 307. Match frontend API paths exactly to backend definitions.

### 6. SSE stream silent on V11 jobs
Check Redis is up and `worker` replicas are running. The router publishes via `v11/event_bus.py`; if Redis is unreachable, the SSE endpoint will hold the connection but emit nothing. `docker compose ps` should show `app`, `worker` (x2), `postgres`, `redis` all healthy.

### 7. Fee values shifted (CT/AT/SF/MF wrong)
Step 12 fee verification (text-based LLM + 7-layer deterministic fallback + auto-revert) handles this. Don't disable any layer; they compose. CT=0 / SF=0 are often genuine, so corrections require positive page-text evidence.

## URLs

- App: http://localhost:9000
- Default login: `admin` / `admin123`
