# RO-ED AI Agent

## Overview

Myanmar customs PDF extraction platform. Each page of a customs declaration is classified and routed to the right specialist — typed pages go to **Veritas** (V7), handwritten pages go to **Scrivener** (V10 PRO) — and the merged result is presented in a side-by-side review UI for human approval. The active production pipeline is **V11 Maestro**: queue-driven (Redis + RQ), Postgres-backed, with live SSE router events. Built by City AI Team — City Holdings Myanmar.

---

## Quick Start

```bash
git clone <repo-url> RO-ED-Lang && cd RO-ED-Lang
cp .env.example .env       # set OPENROUTER_API_KEY + JWT_SECRET_KEY + FERNET_KEY
docker compose up -d --build
```

Open **http://localhost:9000** and log in with `admin` / `admin123` (configurable via `.env`).

`docker compose ps` should show four services healthy: `postgres`, `redis`, `app`, `worker` (replicas: 2).

---

## Architecture

```
                           ┌──────────────────────────────────┐
                           │  SvelteKit 5 frontend (port 9000)│
                           └───────────────┬──────────────────┘
                                           │ HTTPS / SSE
                                           ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │  FastAPI app (uvicorn ×2)                                        │
 │   POST /api/extract-v11 ── 202 ──▶ enqueue RQ                    │
 │   GET  /api/extract-v11/stream/{id}  (SSE, Redis pubsub)         │
 │   /api/review/*  /api/jobs/*  /api/costs/*  /api/settings/*      │
 └──────────────┬─────────────────────────┬────────────────────────┘
                │                         │
                ▼                         ▼
        ┌──────────────┐           ┌──────────────┐
        │ Postgres 16  │           │  Redis       │
        │ jobs / items │           │  RQ queue    │
        │ declarations │           │  pubsub      │
        │ field_edits  │           └──────┬───────┘
        │ activity_log │                  │
        └──────────────┘                  ▼
                                  ┌──────────────────┐
                                  │  RQ worker ×2    │
                                  │                  │
                                  │  V11 Maestro:    │
                                  │   classify pages │
                                  │   split PDF      │
                                  │   ┌────────────┐ │
                                  │   │ Veritas    │ │  typed pages
                                  │   │ (V7)       │ │
                                  │   └────────────┘ │
                                  │   ┌────────────┐ │
                                  │   │ Scrivener  │ │  handwritten
                                  │   │ (V10 PRO)  │ │
                                  │   └────────────┘ │
                                  │   merge          │
                                  │   field bbox     │
                                  │   DB save        │
                                  └─────────┬────────┘
                                            ▼
                              S3-compatible storage
                              (AWS / MinIO / R2 / Wasabi /
                               Backblaze, or local fallback)
```

---

## Features

- **V11 Maestro routing** — per-page classifier sends typed pages to Veritas and handwritten pages to Scrivener, then merges results
- **Async queue** — 202 + `stream_id`; live SSE router events (JOB_START, CLASSIFY, ROUTE, STAGE_START, STAGE_DONE, MERGE, DB_SAVE, DONE, FAIL)
- **Side-by-side review UI** — PDF iframe + editable form + Excel-style item table; inline cell edit, ▲▼🗑 row actions, [+ ADD] row, page-jump 📍
- **Field-edit audit** — every cell change recorded in `field_edits`
- **Auto-approve cron** — hourly job auto-approves above configurable confidence threshold
- **Activity Log v2** — 9 enrichment fields (IP, UA, auth source, severity, duration, status), KPI strip, security tab, JOB events, filter bar
- **Cost tracking** — `tokens_in` / `tokens_out` per job; `/costs` dashboard with dual-axis trend (ECharts) + Excel/CSV export
- **Multi-LDAP** — cascade login, Fernet-encrypted bind passwords, per-user fast-path cache
- **Keycloak OIDC** — RS256 / PKCE
- **Pluggable storage** — S3-compatible (AWS / MinIO / R2 / Wasabi / Backblaze) configured in `/settings/STORAGE`; local fallback
- **Encrypted secrets at rest** — Fernet for LDAP bind passwords and storage secret keys
- **Field bounding boxes** — `fitz.search_for` rectangles attached to extracted values for highlight on the PDF
- **Fee verification chain** — text-based LLM verifier + 7-layer deterministic fallback + auto-revert safety net
- **Self-learning** — user corrections auto-save fee baselines per importer
- **json_schema enforced** — guaranteed valid JSON output from every assembler step
- **REST API** — V11 plus legacy V7 sync endpoint for external integration
- **No Tesseract** — vision-only OCR via Gemini

---

## Configuration

### `.env`

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | yes | OpenRouter key for AI models |
| `JWT_SECRET_KEY` | yes | `openssl rand -hex 32` |
| `FERNET_KEY` | yes | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` — used to encrypt LDAP + storage secrets at rest |
| `POSTGRES_HOST` / `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | yes | Postgres connection (defaults provided in compose) |
| `REDIS_URL` | yes | e.g. `redis://redis:6379/0` |
| `ADMIN_DEFAULT_USERNAME` | no | First-init admin (default `admin`) |
| `ADMIN_DEFAULT_PASSWORD` | no | First-init admin (default `admin123`) |
| `KEYCLOAK_REALM_URL` / `KEYCLOAK_CLIENT_ID` / `KEYCLOAK_CLIENT_SECRET` / `KEYCLOAK_ADMIN_ROLE` | no | Optional SSO; can also be set via `/settings/KEYCLOAK` |

### Storage (`/settings/STORAGE`)

Pick provider (`aws`, `minio`, `r2`, `wasabi`, `backblaze`, or `local`), supply endpoint + bucket + access key + secret key. The secret key is Fernet-encrypted before being written to `storage_config`.

### LDAP (`/settings/LDAP`)

Multiple directories supported with cascade fallback. Bind passwords are Fernet-encrypted in `ldap_configs`. Per-user fast-path caches the matching directory.

---

## API

### Submit a PDF (V11 Maestro)

```bash
curl -X POST http://localhost:9000/api/extract-v11 \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@invoice.pdf"
# 202 Accepted
# {"stream_id": "abc123", "job_id": null, "status": "queued"}
```

### Stream live events (SSE)

```bash
curl -N http://localhost:9000/api/extract-v11/stream/abc123 \
  -H "Authorization: Bearer $TOKEN"
# event: CLASSIFY  data: {"page": 1, "verdict": "PRINTED"}
# event: ROUTE     data: {"printed": [1,2], "inked": [3]}
# event: STAGE_DONE data: {"stage": "veritas", "ms": 41200}
# event: DONE      data: {"job_id": "...", "items": 7}
```

### Poll status

```bash
curl http://localhost:9000/api/extract-v11/status/abc123 \
  -H "Authorization: Bearer $TOKEN"
```

### Legacy synchronous extract (V7)

```bash
curl -X POST http://localhost:9000/api/extract \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@invoice.pdf"
# Returns full JSON when complete
```

### Review API (selected)

```
GET    /api/review/queue
GET    /api/review/stats
POST   /api/review/{job_id}/approve
POST   /api/review/{job_id}/reject
POST   /api/review/{job_id}/draft
POST   /api/review/{job_id}/edit
POST   /api/review/{job_id}/items           (add)
PATCH  /api/review/{job_id}/items/{item_id}
DELETE /api/review/{job_id}/items/{item_id}
POST   /api/review/{job_id}/rerun
POST   /api/review/bulk-approve
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | SvelteKit 5 (runes), TailwindCSS 4.2, ECharts |
| Backend | FastAPI 0.115, Uvicorn (Python 3.12, 2 workers) |
| Database | Postgres 16 (psycopg3 + SQLAlchemy QueuePool) |
| Queue | Redis + RQ (worker replicas: 2) |
| Vision + Assembler | Google Gemini 3 Flash Preview (OpenRouter) |
| Verifier | Anthropic Claude Sonnet 4.6 (OpenRouter) |
| Fee Verifier | Gemini 3 Flash, text-based |
| PDF | PyMuPDF (300 DPI) + Pillow — no Tesseract |
| Auth | Local JWT + multi-LDAP + Keycloak OIDC |
| Storage | S3-compatible (factory) or local |
| Container | docker-compose (postgres + redis + app + worker x2) |

---

## Deployment

### docker-compose (default)

`docker compose up -d --build` provisions four services. Postgres data is volume-mounted; storage is configurable via `/settings/STORAGE`.

### Production notes

- Set strong `JWT_SECRET_KEY`, `FERNET_KEY`, and `POSTGRES_PASSWORD` (never reuse defaults)
- Pin `worker` replicas to expected throughput (each worker handles one V11 job at a time; default `replicas: 2`)
- Postgres pool: SQLAlchemy QueuePool 10 + 10 overflow, 30s timeout — tune via env if you raise app workers
- API semaphore caps OpenRouter calls at 16; raise carefully
- Run Alembic migrations via `docker compose exec app alembic upgrade head`
- Migrating from a SQLite deployment? Use `backend/scripts/migrate_sqlite_to_pg.py`
- Activity Log v2 is append-only — set up a retention policy if you ingest at high volume
- `auto_approve_threshold` and `auto_approve_enabled` live in `app_settings` and are managed in `/settings/AUTO_APPROVE`

### Health check

```bash
curl http://localhost:9000/api/health
```

---

## License

Proprietary. Created by **City AI Team** — City Holdings Myanmar.
