# RO-ED AI Agent

> Myanmar customs PDF → structured data via per-page LLM router, with human review and approval.

## Overview

RO-ED AI Agent is a production document-extraction platform for Myanmar customs declarations. Each PDF is split page-by-page and routed to the right specialist: **typed pages** flow through **Veritas** (V7 pipeline), **handwritten pages** flow through **Scrivener** (V10 PRO pipeline). Results merge into a single declaration plus item table that is presented in a side-by-side review UI for inline editing and approval.

The active production pipeline is **V11 Maestro** — queue-driven (Redis + RQ), Postgres-backed, with live router events streamed over Server-Sent Events. The system is multi-user, supports local + multi-LDAP cascade + Keycloak OIDC authentication, S3-compatible object storage with Fernet-encrypted secrets, daily Postgres backups, optional Sentry observability, and per-IP rate limiting backed by Redis.

Built by City AI Team — City Holdings Myanmar. Designed for a 10-user concurrent operations team.

---

## Quick Start

### Local development

**Prerequisites**

- Docker Desktop ≥ 4.30 (≥ 8 GB RAM allocated, ≥ 12 GB recommended for V11)
- Ports `9080`, `9443`, `5432`, `6379` free on the host

**Steps**

```bash
git clone <repo-url> RO-ED-Lang
cd RO-ED-Lang

cp .env.example .env
# REQUIRED edits in .env:
#   OPENROUTER_API_KEY=sk-or-v1-...
#   JWT_SECRET_KEY=$(openssl rand -hex 32)

bash scripts/generate_dev_cert.sh        # self-signed TLS cert for nginx
docker compose up -d --build

# wait ~30s for migrations + healthchecks, then open:
open https://localhost:9443
```

**First login**

- Username: `admin` (override with `ADMIN_DEFAULT_USERNAME`)
- Password:
  - If `ADMIN_INITIAL_PASSWORD` is set in `.env`, use that.
  - Otherwise a random password is printed once on first boot:
    ```bash
    docker logs ro-ed-api 2>&1 | grep -A2 "INITIAL ADMIN PASSWORD"
    ```
    The admin will be force-redirected to `/change-password` on first login.

### Production deployment (Linux server)

**Recommended hardware**: 16-core / 32 GB RAM / 200 GB SSD for ~10 concurrent users.

```bash
# 1. Install Docker + compose plugin
curl -fsSL https://get.docker.com | sh

# 2. Clone + configure
git clone <repo-url> /opt/ro-ed && cd /opt/ro-ed
cp .env.example .env
$EDITOR .env
#   OPENROUTER_API_KEY=...
#   JWT_SECRET_KEY=$(openssl rand -hex 32)        # ≥ 32 chars enforced
#   PG_PASSWORD=$(openssl rand -hex 24)
#   DEV_MODE=                                     # MUST be empty in prod
#   CORS_ALLOWED_ORIGINS=https://ro-ed.your-domain.com
#   SENTRY_DSN=https://...                        # optional
#   LDAP_FERNET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
#   STORAGE_FERNET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# 3. TLS certificate
#    Replace nginx/ssl/fullchain.pem + nginx/ssl/privkey.pem with Let's Encrypt
#    or commercial cert. Filenames must match nginx/conf.d/*.conf.

# 4. Boot
docker compose up -d

# 5. Configure via UI: log in as admin → Settings → STORAGE / LDAP / KEYCLOAK
```

### Docker compose commands

```bash
docker compose up -d                 # start everything
docker compose down                  # stop (preserves volumes)
docker compose down -v               # stop + delete data (DESTRUCTIVE)
docker compose logs -f app           # tail API logs
docker compose logs -f worker        # tail RQ worker logs
docker compose restart app worker    # restart after backend code change
docker compose up -d --build         # rebuild image (after Dockerfile / requirements change)
docker compose ps                    # service status + healthchecks
```

---

## Architecture

```
                    ┌────────────────────────────┐
  Browser  HTTPS    │       Nginx 1.27           │  9080 → 9443 (redirect)
  ───────────────►  │   TLS 1.2/1.3 + HSTS       │
                    └──────────┬─────────────────┘
                               │ proxy_pass (SSE: buffering off, timeouts long)
                               ▼
                    ┌────────────────────────────┐
                    │  FastAPI app  (uvicorn ×2) │
                    │  slowapi rate-limit         │
                    │  Sentry (optional)          │
                    └──┬────┬──────────────┬─────┘
                       │    │              │
              POST /v11│    │ SSE stream   │ enqueue RQ job
                       │    │              ▼
                       │    │      ┌──────────────────────┐
                       │    │      │   Redis 7             │ ◄──── pub/sub channel
                       │    └─────►│   pubsub + RQ queue   │
                       │           └─────────┬─────────────┘
                       │                     │ worker dequeue
                       ▼                     ▼
                ┌──────────────────────────────────────┐
                │  V11 Maestro workflow                │
                │   1. PageClassifier (per-page)       │
                │   2. pdf_split (PRINTED / INKED)     │
                │   3. PARALLEL                        │  ◄── OpenRouter API
                │       ├─ Veritas  (V7) typed pages   │      (Gemini, Claude)
                │       └─ Scrivener (V10 PRO)         │
                │   4. Merger                          │
                │   5. field_bbox (PyMuPDF search_for) │
                │   6. DB save → DONE                  │
                └────────────┬─────────────────────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        Postgres 16     Redis 7        S3 / Local
        (jobs, items,   (queue,        (PDFs, exports,
         declarations,   pubsub)        backups)
         field_edits,
         activity_log,
         users, groups,
         ldap_configs,
         storage_config)

        pg-backup                       (cron loop in container,
         (sidecar)                       daily pg_dump → /backups,
                                         14-day retention)
```

**Stack ports**

| Service     | Port (host)         | Visibility       |
|-------------|---------------------|------------------|
| nginx       | `9080` (HTTP→443)   | user-facing      |
| nginx       | `9443` (HTTPS)      | user-facing      |
| app         | —                   | internal only    |
| worker × 2  | —                   | internal only    |
| postgres    | —                   | internal only    |
| redis       | —                   | internal only    |
| pg-backup   | —                   | internal only    |

---

## Features

### V11 Maestro pipeline

Per-page classifier routes each page to the right specialist, runs Veritas + Scrivener in parallel, then merges. Live SSE events: `JOB_START`, `CLASSIFY`, `ROUTE`, `STAGE_START`, `STAGE_DONE`, `MERGE`, `DB_SAVE`, `DONE`, `FAIL`.

### Side-by-side review / approve workflow

PDF iframe + editable form + Excel-style item table. Inline cell edit, ▲▼🗑 row actions, [+ ADD] row, page-jump 📍 hot-link from any field to the source page in the PDF. Statuses: `pending_review`, `approved`, `rejected`, `draft`. Every cell change is recorded in `field_edits` with old/new value, user, timestamp.

### Multi-LDAP authentication (cascade)

Configure any number of LDAP servers via Settings → LDAP. Login attempts cascade through active configs in priority order. Bind passwords are Fernet-encrypted at rest. Per-user fast-path cache remembers the last successful LDAP for each user.

### Keycloak SSO

OIDC RS256 + PKCE. Configure via `KEYCLOAK_*` env vars or the Settings → AUTH UI. The `/api/auth/config` endpoint exposes OIDC config to the frontend.

### S3-compatible storage

Configurable from Settings → STORAGE. Providers: AWS S3, MinIO, Cloudflare R2, Wasabi, Backblaze B2, or any custom S3-compatible endpoint. Multiple configs allowed; one is "active" at a time. Secrets Fernet-encrypted in DB. Falls back to local volume if no active config.

### Activity Log v2

9-field enrichment per event (auth_source, status, duration_ms, resource, severity, error_message, payload_json, user_agent, ip_address). Severity-aware drawer UI in `/settings/ACTIVITY_LOG` with a dedicated SECURITY tab. Includes JOB_* events from V11 worker. CSV export.

### Auto-approve cron

Hourly sweep of `pending_review` jobs. If a job's confidence score crosses the configured threshold, it is auto-approved. Configure in Settings → AUTO_APPROVE.

### Cost tracking + tokens dashboard

Per-job, per-pipeline-stage `cost_usd`, `tokens_in`, `tokens_out`. Endpoints under `/api/usage` plus an ECharts dashboard at `/costs`.

### Excel / CSV export

`/api/data/items/download` and `/api/data/declarations/download` produce Excel workbooks. `/api/activity/export/csv` for activity logs.

### HTTPS via nginx

Self-signed for dev (`scripts/generate_dev_cert.sh`); Let's Encrypt or commercial cert for prod. TLS 1.2/1.3 only, HSTS enabled. SSE has buffering disabled and long timeouts.

### Daily Postgres backups

`pg-backup` sidecar container runs `scripts/pg_backup_loop.sh`. `pg_dump` daily at 02:00 UTC, gzip, 14-day retention. Volume `pg-backups`. Optional S3 upload via `S3_BACKUP_BUCKET`.

### Rate limiting

slowapi backed by Redis (shared across uvicorn workers). Defaults:

- `POST /api/auth/login` — 5 / minute (anti-brute-force)
- `POST /api/extract*` — 10 / minute
- Global — 1000 / hour per IP

Override with `RATE_LIMIT_LOGIN`, `RATE_LIMIT_EXTRACT` in `.env`.

### Sentry observability (optional)

Set `SENTRY_DSN` to enable. Both FastAPI and the RQ worker container ship uncaught exceptions and a sample of traces. Integrations: Starlette, AsyncIO, Redis, SQLAlchemy, RQ. `HTTPException` and `RequestValidationError` are filtered out. PII off (`send_default_pii=False`).

---

## Configuration

### `.env` reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENROUTER_API_KEY` | yes | — | OpenRouter API key for all LLM calls |
| `JWT_SECRET_KEY` | yes (prod) | — | Token signing secret. **Must be ≥ 32 chars.** Generate: `openssl rand -hex 32` |
| `DEV_MODE` | no | `1` | When `1`, allows blank `JWT_SECRET_KEY` fallback for local dev. **Empty for prod.** |
| `ADMIN_DEFAULT_USERNAME` | no | `admin` | Initial admin username on first DB init |
| `ADMIN_INITIAL_PASSWORD` | no | (random) | If blank, a random password is printed once and admin must change on first login |
| `PG_PASSWORD` | yes | `ro_ed_dev_pass` | Postgres password |
| `DATABASE_URL` | yes | `postgresql+psycopg://ro_ed:${PG_PASSWORD}@postgres:5432/ro_ed` | SQLAlchemy URL |
| `REDIS_URL` | yes | `redis://redis:6379/0` | Used by RQ + pubsub + slowapi |
| `CORS_ALLOWED_ORIGINS` | yes (prod) | `http://localhost:5173,...,https://localhost` | Comma-separated origins |
| `RATE_LIMIT_LOGIN` | no | `5/minute` | Login rate limit override |
| `RATE_LIMIT_EXTRACT` | no | `10/minute` | Extract rate limit override |
| `KEYCLOAK_REALM_URL` | no | — | e.g. `https://kc.example.com/realms/ro-ed` |
| `KEYCLOAK_CLIENT_ID` | no | — | OIDC client id |
| `KEYCLOAK_CLIENT_SECRET` | no | — | OIDC client secret |
| `KEYCLOAK_ADMIN_ROLE` | no | `admin` | Realm role mapped to admin |
| `SENTRY_DSN` | no | — | Enable Sentry when set |
| `SENTRY_ENVIRONMENT` | no | `development` | Sentry env tag |
| `SENTRY_RELEASE` | no | `ro-ed@dev` | Sentry release tag |
| `SENTRY_TRACES_SAMPLE_RATE` | no | `0.1` | 0..1 |
| `SENTRY_PROFILES_SAMPLE_RATE` | no | `0.0` | 0..1 |
| `S3_BACKUP_BUCKET` | no | — | If set, pg-backup uploads dumps to this S3 bucket |
| `LDAP_FERNET_KEY` | no | (auto) | Fernet key for encrypting LDAP bind passwords. Generate once, keep stable. |
| `STORAGE_FERNET_KEY` | no | (auto) | Fernet key for encrypting S3 secret keys. Generate once, keep stable. |

### S3 storage configuration

1. Log in as admin.
2. Navigate to Settings → STORAGE → **+ ADD STORAGE**.
3. Pick provider: **AWS / MinIO / R2 / Wasabi / Backblaze / Custom**.
4. Fill in `endpoint_url`, `region`, `bucket`, `access_key_id`, `secret_access_key`, `key_prefix`.
5. Choose usage flags: `use_for_uploads`, `use_for_exports`, `use_for_cache`, `use_for_archive`.
6. Click **TEST** (uploads + reads + deletes a small probe object).
7. Click **ACTIVATE** to make it the live config.

CLI alternative:

```bash
curl -X POST https://localhost:9443/api/storage/configs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"label":"prod-r2","provider":"r2","endpoint_url":"https://<acct>.r2.cloudflarestorage.com",
       "bucket_name":"ro-ed-pdfs","access_key_id":"...","secret_access_key":"...",
       "use_for_uploads":1,"use_for_exports":1}'
```

### LDAP configuration

1. Settings → LDAP → **+ ADD LDAP**.
2. Fill in: `host`, `port` (389 / 636), `use_tls`, `bind_dn`, `bind_password`, `search_base`, `user_filter`.
3. Set `priority` (lower = tried first) and `active=true`.
4. Click **TEST** (probes bind + sample search).

Multi-LDAP cascade: when a user logs in, configs are tried in priority order. On first match the user is upserted with `auth_source=ldap`, `default_ldap_id=<that config>`. Future logins try that config first (fast-path), then fall through.

### Keycloak SSO

```
KEYCLOAK_REALM_URL=https://your-kc.example.com/realms/ro-ed
KEYCLOAK_CLIENT_ID=ro-ed-client
KEYCLOAK_CLIENT_SECRET=...
KEYCLOAK_ADMIN_ROLE=admin
```

Or do it in the UI: Settings → KEYCLOAK. The frontend reads `/api/auth/config` to discover OIDC and renders an SSO button on `/login`.

### Auto-approve threshold

Settings → AUTO_APPROVE → enable + set threshold (e.g. `0.95`). Hourly cron in `main.py` lifespan sweeps `review_status='pending_review'` jobs whose `accuracy_percent` clears the bar.

---

## API

### Authentication

```
POST /api/auth/login            body: {username, password}
                                → 200 + {access_token, user, must_change_password}
POST /api/auth/token            OIDC code exchange (PKCE)
POST /api/auth/refresh          OIDC refresh
POST /api/auth/change-password  body: {current_password, new_password}
GET  /api/auth/me               Authorization: Bearer <jwt>
GET  /api/auth/config           OIDC discovery for frontend
POST /api/auth/logout
```

### V11 extraction (queue-based)

```
POST /api/extract-v11           multipart: file, job_id (optional)
                                → 202 + {stream_id, job_id, queue_position, message}

GET  /api/extract-v11/stream/{stream_id}    text/event-stream
                                → events: JOB_START, CLASSIFY, ROUTE,
                                          STAGE_START, STAGE_DONE,
                                          MERGE, DB_SAVE, DONE, FAIL

GET  /api/extract-v11/status/{stream_id}
                                → {status: queued|started|finished|failed,
                                   result: {job_id, ...}}
```

### Legacy / specialty extraction

```
POST /api/extract               V7 sync (legacy external integrations)
POST /api/extract-v10-pro       V10 PRO standalone (HW testing)
```

### curl example — full submit + stream + fetch

```bash
TOKEN=$(curl -sk https://localhost:9443/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"..."}' | jq -r .access_token)

# 1. submit
RESP=$(curl -sk -X POST https://localhost:9443/api/extract-v11 \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@sample.pdf")
STREAM_ID=$(echo "$RESP" | jq -r .stream_id)

# 2. live events
curl -Nsk -H "Authorization: Bearer $TOKEN" \
  https://localhost:9443/api/extract-v11/stream/$STREAM_ID

# 3. final status
curl -sk -H "Authorization: Bearer $TOKEN" \
  https://localhost:9443/api/extract-v11/status/$STREAM_ID | jq .

# 4. result
JOB_ID=$(curl -sk -H "Authorization: Bearer $TOKEN" \
  https://localhost:9443/api/extract-v11/status/$STREAM_ID | jq -r .result.job_id)
curl -sk -H "Authorization: Bearer $TOKEN" \
  https://localhost:9443/api/jobs/$JOB_ID | jq .
```

### Review API

```
GET    /api/review/queue                         filtered list
GET    /api/review/stats                         counts per status
GET    /api/review/{job_id}                      full job + edits
PATCH  /api/review/{job_id}/declaration          inline cell edit (declaration)
PATCH  /api/review/{job_id}/items/{item_index}   inline cell edit (item row)
POST   /api/review/{job_id}/approve
POST   /api/review/{job_id}/reject
POST   /api/review/{job_id}/draft
POST   /api/review/bulk/approve                  body: {job_ids: [...]}
POST   /api/review/bulk/reject                   body: {job_ids: [...]}
GET    /api/review/{job_id}/edits                audit trail
POST   /api/review/{job_id}/items                add new row
DELETE /api/review/{job_id}/items/{item_index}   soft-delete row
POST   /api/review/{job_id}/items/reorder        body: {order: [...]}
POST   /api/review/{job_id}/rerun                re-extract; links via parent_job_id
```

### Other routes (mounted in `main.py`)

```
/api/jobs           list, detail, upload, upload-batch, pages, page-image,
                    annotated-pdf, pdf, preview-pdf, download, confidence
/api/users          CRUD + activity-logs
/api/groups         CRUD + assign user
/api/data           items, declarations, search, stats, cost-stats,
                    items/download, declarations/download, ai-tables
/api/usage          summary, per-doc, by-type, by-pipeline
/api/settings       auto-approve, keycloak (get / put / test)
/api/ldap           configs CRUD + test
/api/storage        configs CRUD + activate + test + active
/api/activity       list, security, stats, detail, export/csv
/api/corrections    submit, list, stats, by-job
/api/health         healthcheck
```

---

## Operations

### Backups

- **Schedule** — daily at 02:00 UTC, in container `ro-ed-pg-backup`.
- **Format** — `pg_dump` → gzip → `/backups/ro_ed_YYYYMMDD_HHMMSS.sql.gz`.
- **Retention** — 14 days (configurable via `RETENTION_DAYS` env on the sidecar).
- **Volume** — `pg-backups` (Docker named volume; mount to host path for off-box copies).
- **Manual backup**:
  ```bash
  bash scripts/pg_backup_now.sh
  ```
- **Restore**:
  ```bash
  bash scripts/pg_restore.sh ro_ed_20260506_020000.sql.gz
  ```
- **Off-site** — set `S3_BACKUP_BUCKET` to push dumps to S3 (requires aws-cli in the backup image; off by default).

### Monitoring

```
SENTRY_DSN=https://<key>@<ingest>.ingest.sentry.io/<project>
SENTRY_ENVIRONMENT=production
SENTRY_RELEASE=ro-ed@<git-sha>
SENTRY_TRACES_SAMPLE_RATE=0.1
```

Integrations enabled when DSN set: FastAPI, Starlette, AsyncIO, Redis, SQLAlchemy, RQ. `HTTPException` + `RequestValidationError` filtered out.

### Health checks

- `GET /api/health` → `200 {"status":"ok"}`
- Docker healthchecks on `postgres` (`pg_isready`), `redis` (`redis-cli ping`), `app` (`curl /api/health`), `nginx` (`wget /health`).
- Use `docker compose ps` to see status at a glance.

### Logs

```
docker logs ro-ed-api                 # FastAPI
docker logs ro-ed-lang-worker-1       # RQ worker replica 1
docker logs ro-ed-lang-worker-2       # RQ worker replica 2
docker logs ro-ed-nginx               # nginx
docker logs ro-ed-postgres
docker logs ro-ed-redis
docker logs ro-ed-pg-backup
```

Nginx access + error logs are persisted to `nginx/logs/` on the host.

### Rate-limit overrides

```
RATE_LIMIT_LOGIN=20/minute
RATE_LIMIT_EXTRACT=30/minute
```

---

## Tech Stack

- **Frontend:** SvelteKit 5 (runes), TailwindCSS 4.2, ECharts (cost dashboard), Vite
- **Backend:** FastAPI 0.115, Uvicorn (Python 3.12, 2 workers), Pydantic v2
- **Database:** Postgres 16, psycopg3, SQLAlchemy QueuePool (10 base + 10 overflow)
- **Migrations:** Alembic (`backend/alembic/`)
- **Queue:** RQ + Redis 7 (worker container, 2 replicas)
- **Reverse proxy:** Nginx 1.27 with HTTPS (TLS 1.2/1.3, HSTS)
- **Auth:** PyJWT (HS256), python-ldap3, Keycloak OIDC (RS256 / PKCE)
- **Storage:** boto3 (S3-compatible), local filesystem fallback, Fernet (cryptography)
- **PDF:** PyMuPDF (fitz) at 300 DPI, Pillow
- **LLMs:** OpenRouter API (Gemini 3 Flash Preview, Claude Sonnet 4.6 Verifier)
- **Rate limiting:** slowapi (Redis storage)
- **Observability:** Sentry SDK (optional)

---

## Project Structure

```
RO-ED-Lang/
├── backend/
│   ├── main.py                       FastAPI app, extract endpoints, lifespan, auto-approve cron
│   ├── worker.py                     RQ worker entrypoint
│   ├── auth.py                       JWT + password hashing + OIDC verification
│   ├── ldap_auth.py                  Multi-LDAP cascade login
│   ├── ldap_crypto.py                Fernet wrapper for LDAP bind passwords
│   ├── database.py                   Postgres-backed sqlite-compat shim
│   ├── db_engine.py                  SQLAlchemy QueuePool, dict-row factory
│   ├── schemas.py                    Pydantic models
│   ├── config.py                     Env loading + validation
│   ├── middleware.py                 Logging + CORS + Sentry hooks
│   ├── rate_limit.py                 slowapi config
│   ├── event_logger.py               Activity log writer
│   ├── cost_tracker.py               Token + USD accounting
│   ├── alembic/                      DB migrations
│   ├── routes/                       auth, jobs, users, groups, data, settings,
│   │                                 corrections, usage, ldap, activity, storage, review
│   ├── jobs/                         queue.py, tasks.py (RQ singletons + run_v11_task)
│   ├── v11/                          V11 Maestro
│   │   ├── workflow.py               orchestrator
│   │   ├── event_bus.py              Redis pubsub for SSE
│   │   ├── agents/                   page_classifier.py, merger.py
│   │   └── tools/                    pdf_split.py, field_bbox.py
│   ├── v10_pro/                      Scrivener (handwritten pipeline)
│   ├── pipeline/                     V7 / Veritas (typed-page pipeline)
│   ├── v2/                           confidence + validation + report
│   ├── storage/                      local.py, s3.py (factory)
│   └── scripts/                      migrate_sqlite_to_pg.py
├── frontend/
│   └── src/
│       ├── routes/                   agent, login, change-password, review,
│       │                             history, declarations, items, costs,
│       │                             users, settings
│       └── lib/                      api.ts, components/, stores/, utils/, colors.ts
├── nginx/
│   ├── conf.d/                       server blocks (HTTP→HTTPS redirect, HTTPS proxy)
│   ├── ssl/                          fullchain.pem + privkey.pem
│   └── logs/                         persisted access + error logs
├── scripts/
│   ├── generate_dev_cert.sh          self-signed dev TLS
│   ├── pg_backup_loop.sh             daily cron (used inside pg-backup container)
│   ├── pg_backup_now.sh              manual one-shot backup
│   └── pg_restore.sh                 restore <file>.sql.gz
├── docker-compose.yml
├── Dockerfile
└── .env.example
```

---

## Development

### Frontend dev server

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, proxies /api → :9000
```

### Backend dev (no docker)

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export DATABASE_URL=postgresql+psycopg://ro_ed:pw@localhost:5432/ro_ed
export REDIS_URL=redis://localhost:6379/0
export OPENROUTER_API_KEY=...
export JWT_SECRET_KEY=$(openssl rand -hex 32)

# Terminal A — API
uvicorn main:app --reload --port 9000

# Terminal B — RQ worker
python worker.py
```

### Tests / quality gates

```bash
python3 -m py_compile backend/main.py        # syntax sanity
cd frontend && npm run build                  # type + build check
# E2E: drop a PDF on /agent → confirm result lands in /review
```

### Adding migrations

```bash
cd backend
alembic revision -m "your migration name"
# edit alembic/versions/<id>_your_migration_name.py
alembic upgrade head
```

---

## Roadmap

- Frontend Sentry SDK (Sentry SvelteKit)
- Email notifications on job completion / failure
- Bulk PDF upload (zip → fan-out)
- Per-importer dashboard
- Multi-tenant scoping

---

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `res.json()` hangs (Brave browser) | Use `res.text()` + `JSON.parse()`; Brave's privacy fetch shim deadlocks on empty bodies. |
| Svelte 5 `{@const}` error | `{@const}` must live inside `{#if}` / `{#each}` / `<Component>` blocks, not at top level. |
| `500` on a slowapi-decorated endpoint | The handler must declare `request: Request` (or `response: Response`) as a parameter — slowapi reads the IP off it. |
| `database is locked` retries | Was a SQLite artefact. Postgres + advisory locks fixes it. If you still see it, you're hitting the legacy path — re-run migrations. |
| SSE stream silent | Check Redis pubsub: `docker exec ro-ed-redis redis-cli SUBSCRIBE 'job:*'`. Confirm nginx has `proxy_buffering off` for `/api/extract-v11/stream`. |
| OOM during V11 | Bump Docker Desktop RAM to ≥ 12 GB; raise worker `mem_limit` in `docker-compose.yml`. |
| `JWT_SECRET_KEY too short` on prod boot | Must be ≥ 32 chars. Regenerate: `openssl rand -hex 32`. `DEV_MODE=` (empty) in prod. |
| Login storm 429s | Bump `RATE_LIMIT_LOGIN=20/minute`. |
| `LDAP_FERNET_KEY` rotated → existing passwords unreadable | Keys must be stable. Treat them as long-lived secrets; back them up. |

---

## License

City AI Team — City Holdings Myanmar (proprietary).
