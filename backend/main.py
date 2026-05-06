#!/usr/bin/env python3
"""
RO-ED AI Agent — FastAPI Backend
Myanmar PDF Data Extraction Pipeline
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from typing import Optional
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

import database
import event_logger


async def _auto_approve_loop():
    """Periodic task: auto-approve high-confidence pending jobs.

    Runs hourly. Only acts when settings flag enabled. A job is approved if its
    accuracy_percent >= threshold * 100. Marked reviewed_by='SYSTEM_AUTO'.
    """
    while True:
        try:
            enabled = (database.get_app_setting("auto_approve_enabled", "false") or "false").lower() == "true"
            if enabled:
                try:
                    threshold = float(database.get_app_setting("auto_approve_threshold", "0.95") or 0.95)
                except (TypeError, ValueError):
                    threshold = 0.95
                cutoff = threshold * 100.0
                jobs = database.list_review_queue(status="pending_review", limit=500)
                approved = 0
                for j in jobs:
                    acc = j.get("accuracy_percent") or 0
                    if acc >= cutoff:
                        try:
                            ok = database.update_review_status(
                                j["job_id"], "approved",
                                reviewed_by="SYSTEM_AUTO",
                                notes=f"Auto-approved at threshold {threshold}",
                            )
                            if ok:
                                approved += 1
                                event_logger.log_event(
                                    action="JOB_AUTO_APPROVED",
                                    user="SYSTEM_AUTO",
                                    status="OK",
                                    resource=j["job_id"],
                                    details=f"accuracy={acc} threshold={cutoff}",
                                    payload={"threshold": threshold, "accuracy_percent": acc},
                                )
                        except Exception as e:
                            print(f"[auto_approve] job {j.get('job_id')} error: {e}")
                database.set_app_setting(
                    "auto_approve_last_run",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
                database.set_app_setting("auto_approve_last_count", approved)
                print(f"[auto_approve] swept {len(jobs)} pending — approved {approved} (threshold={threshold})")
        except Exception as e:
            print(f"[auto_approve] error: {e}")
        await asyncio.sleep(3600)  # hourly


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database + spawn auto-approve background task."""
    database.init_database()
    task = asyncio.create_task(_auto_approve_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(
    title="RO-ED AI Agent",
    version="3.0.0",
    lifespan=lifespan,
)

# CORS — allow frontend dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8000", "http://localhost:9000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Phase 7: capture client IP + User-Agent into request.state for every request
try:
    from middleware import EventContextMiddleware
    app.add_middleware(EventContextMiddleware)
except Exception:
    pass

# --- API Routes ---
from routes import auth as auth_routes
from routes import jobs as job_routes
from routes import users as user_routes
from routes import data as data_routes
from routes import ws as ws_routes
from routes import settings as settings_routes
from routes import groups as group_routes
from routes import corrections as correction_routes
from routes import usage as usage_routes
from routes import ldap as ldap_routes
from routes import activity as activity_routes
from routes import storage as storage_routes
from routes import review as review_routes
app.include_router(auth_routes.router, prefix="/api/auth", tags=["auth"])
app.include_router(job_routes.router, prefix="/api/jobs", tags=["jobs"])
app.include_router(user_routes.router, prefix="/api/users", tags=["users"])
app.include_router(data_routes.router, prefix="/api/data", tags=["data"])
app.include_router(ws_routes.router, prefix="/api/ws", tags=["pipeline"])
app.include_router(settings_routes.router, prefix="/api/settings", tags=["settings"])
app.include_router(group_routes.router, prefix="/api/groups", tags=["groups"])
app.include_router(correction_routes.router, prefix="/api/corrections", tags=["corrections"])
app.include_router(usage_routes.router, prefix="/api/usage", tags=["usage"])
app.include_router(ldap_routes.router, prefix="/api/ldap", tags=["ldap"])
app.include_router(activity_routes.router, prefix="/api/activity", tags=["activity"])
app.include_router(storage_routes.router, prefix="/api/storage", tags=["storage"])
app.include_router(review_routes.router, prefix="/api/review", tags=["review"])


@app.post("/api/extract")
async def extract_pdf(
    file: UploadFile = File(...),
    mode: str = "ro_ed",
):
    """T4.1 — REST API Mode: Upload PDF, run extraction, return JSON results.
    No WebSocket needed. For external system integration.

    Args:
        file: PDF file upload
        mode: pipeline mode
    """
    import asyncio
    import shutil
    import uuid

    if file is None:
        raise HTTPException(400, "No file uploaded")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files accepted")

    # Save to temp
    import config
    config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    save_path = config.UPLOAD_FOLDER / safe_name
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        from pipeline.pipeline import run_pipeline
        result = await asyncio.to_thread(run_pipeline, str(save_path))

        if not result:
            raise HTTPException(500, "Extraction failed")

        return {
            "status": "ok",
            "filename": file.filename,
            "mode": mode,
            "items_count": result.get("items_count", len(result.get("items", []))),
            "accuracy": result.get("accuracy", 0),
            "duration": result.get("duration_seconds", 0),
            "cost": result.get("cost_usd", 0),
            "tokens_in": result.get("tokens_in", 0),
            "tokens_out": result.get("tokens_out", 0),
            "cost_breakdown": result.get("cost_breakdown", []),
            "declaration": result.get("declaration", {}),
            "items": result.get("items", []),
            "cross_validation": result.get("cross_validation"),
        }
    finally:
        # Cleanup temp file
        try:
            save_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.post("/api/extract-v8")
async def extract_pdf_v8(file: UploadFile = File(...)):
    """V8 — Claude-only 5-pass pipeline (Haiku filter → Opus → Sonnet → Opus reconciler → post-fix)."""
    import asyncio
    import shutil
    import uuid

    if file is None:
        raise HTTPException(400, "No file uploaded")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files accepted")

    import config
    config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    save_path = config.UPLOAD_FOLDER / safe_name
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        try:
            from v8.cc_pipeline import run_one
        except Exception as e:
            raise HTTPException(500, f"V8 pipeline unavailable: {e}")

        try:
            result = await asyncio.to_thread(run_one, str(save_path))
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"V8 extraction failed: {e}")

        if not result:
            raise HTTPException(500, "V8 extraction returned no result")

        return {
            "status": "ok",
            "filename": file.filename,
            "mode": "v8",
            "items_count": len(result.get("items", [])),
            "duration": result.get("duration_seconds", 0),
            "cost": result.get("cost_usd", 0),
            "declaration": result.get("declaration", {}),
            "items": result.get("items", []),
            "document_format": result.get("document_format", ""),
            "pages_kept": result.get("pages_kept", []),
            "cost_breakdown": result.get("cost_breakdown", []),
            "anomalies": result.get("anomalies", []),
            "postfix_notes": result.get("postfix_notes", []),
        }
    finally:
        try:
            save_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.post("/api/extract-v9")
async def extract_pdf_v9(file: UploadFile = File(...)):
    """V9 — Agno agentic pipeline (Haiku filter → Gemini-Pro reader → Sonnet verifier → post-fix)."""
    import asyncio
    import shutil
    import uuid

    if file is None:
        raise HTTPException(400, "No file uploaded")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files accepted")

    import config
    config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    save_path = config.UPLOAD_FOLDER / safe_name
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        try:
            from v9.workflow import run as run_v9
        except Exception as e:
            raise HTTPException(500, f"V9 pipeline unavailable: {e}")

        try:
            result = await asyncio.to_thread(run_v9, str(save_path))
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"V9 extraction failed: {e}")

        if not result:
            raise HTTPException(500, "V9 extraction returned no result")

        return {
            "status": "ok",
            "filename": file.filename,
            "mode": "v9",
            "items_count": len(result.get("items", [])),
            "duration": result.get("duration_seconds", 0),
            "cost": result.get("cost_usd", 0),
            "declaration": result.get("declaration", {}),
            "items": result.get("items", []),
            "document_format": result.get("document_format", ""),
            "pages_kept": result.get("pages_kept", []),
            "anomalies": result.get("anomalies", []),
            "postfix_notes": result.get("postfix_notes", []),
            "trace": result.get("trace", []),
        }
    finally:
        try:
            save_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.post("/api/extract-v9-pro")
async def extract_pdf_v9_pro(file: UploadFile = File(...)):
    """V9 PRO — Agno agentic pipeline (Haiku filter → Gemini-Pro reader → Sonnet verifier → post-fix)."""
    import asyncio
    import shutil
    import uuid

    if file is None:
        raise HTTPException(400, "No file uploaded")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files accepted")

    import config
    config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    save_path = config.UPLOAD_FOLDER / safe_name
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        try:
            from v9_pro.workflow import run as run_v9pro
        except Exception as e:
            raise HTTPException(500, f"V9 pipeline unavailable: {e}")

        try:
            result = await asyncio.to_thread(run_v9pro, str(save_path))
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"V9 extraction failed: {e}")

        if not result:
            raise HTTPException(500, "V9 extraction returned no result")

        return {
            "status": "ok",
            "filename": file.filename,
            "mode": "v9_pro",
            "items_count": len(result.get("items", [])),
            "duration": result.get("duration_seconds", 0),
            "cost": result.get("cost_usd", 0),
            "declaration": result.get("declaration", {}),
            "items": result.get("items", []),
            "document_format": result.get("document_format", ""),
            "pages_kept": result.get("pages_kept", []),
            "anomalies": result.get("anomalies", []),
            "postfix_notes": result.get("postfix_notes", []),
            "trace": result.get("trace", []),
            "needs_review": result.get("needs_review", False),
        }
    finally:
        try:
            save_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.post("/api/extract-v10")
async def extract_pdf_v10(file: UploadFile = File(...)):
    """V10 — Handwriting Specialist (HW detect → multi-DPI 3-model vote → memory)."""
    import asyncio
    import shutil
    import uuid

    if file is None:
        raise HTTPException(400, "No file uploaded")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files accepted")

    import config
    config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    save_path = config.UPLOAD_FOLDER / safe_name
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        try:
            from v10.workflow import run as run_v10
        except Exception as e:
            raise HTTPException(500, f"V10 pipeline unavailable: {e}")

        try:
            result = await asyncio.to_thread(run_v10, str(save_path))
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"V10 extraction failed: {e}")

        if not result:
            raise HTTPException(500, "V10 extraction returned no result")

        return {
            "status": "ok",
            "filename": file.filename,
            "mode": "v10",
            "items_count": len(result.get("items", [])),
            "duration": result.get("duration_seconds", 0),
            "cost": result.get("cost_usd", 0),
            "declaration": result.get("declaration", {}),
            "items": result.get("items", []),
            "document_format": result.get("document_format", ""),
            "is_handwritten": result.get("is_handwritten", False),
            "pages_kept": result.get("pages_kept", []),
            "anomalies": result.get("anomalies", []),
            "postfix_notes": result.get("postfix_notes", []),
            "trace": result.get("trace", []),
            "needs_review": result.get("needs_review", False),
        }
    finally:
        try:
            save_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.post("/api/extract-v10-pro")
async def extract_pdf_v10_pro(file: UploadFile = File(...)):
    """V10 PRO — HW with shape-validation + memory + 800 DPI digit-list + box detection."""
    import asyncio, shutil, uuid

    if file is None:
        raise HTTPException(400, "No file uploaded")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files accepted")

    import config
    config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    save_path = config.UPLOAD_FOLDER / safe_name
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        try:
            from v10_pro.workflow import run as run_v10_pro
        except Exception as e:
            raise HTTPException(500, f"V10 PRO pipeline unavailable: {e}")
        try:
            result = await asyncio.to_thread(run_v10_pro, str(save_path))
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"V10 PRO extraction failed: {e}")
        if not result:
            raise HTTPException(500, "V10 PRO returned no result")
        return {
            "status": "ok",
            "filename": file.filename,
            "mode": "v10_pro",
            "items_count": len(result.get("items", [])),
            "duration": result.get("duration_seconds", 0),
            "cost": result.get("cost_usd", 0),
            "tokens_in": result.get("tokens_in", 0),
            "tokens_out": result.get("tokens_out", 0),
            "cost_breakdown": result.get("cost_breakdown", []),
            "declaration": result.get("declaration", {}),
            "items": result.get("items", []),
            "document_format": result.get("document_format", ""),
            "is_handwritten": result.get("is_handwritten", False),
            "pages_kept": result.get("pages_kept", []),
            "anomalies": result.get("anomalies", []),
            "postfix_notes": result.get("postfix_notes", []),
            "trace": result.get("trace", []),
            "needs_review": result.get("needs_review", False),
        }
    finally:
        try:
            save_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.post("/api/extract-v11", status_code=202)
async def extract_pdf_v11(file: UploadFile = File(...), job_id: Optional[str] = Form(None)):
    """V11 — Master Router. Queues extraction; returns immediately with job_id.

    Behavior change: this endpoint no longer blocks for 90-180s. It saves the PDF,
    pre-creates a DB job row, enqueues a background RQ task, and returns 202 with
    {job_id, stream_id, queue_position}. Clients should:
      - subscribe to /api/extract-v11/stream/{stream_id} for live router events, and/or
      - poll /api/extract-v11/status/{stream_id} for queue/worker status, then
      - fetch final result via /api/jobs/{job_id} once status == 'finished'.
    """
    import shutil, uuid

    if file is None:
        raise HTTPException(400, "No file uploaded")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files accepted")

    import config
    config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    save_path = config.UPLOAD_FOLDER / safe_name
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # NOTE: do NOT pre-create a DB row here. Worker's V11._save_to_db creates the
    # final job_id when it runs. Frontend uses status_res.result.job_id to fetch.
    db_job_id = None  # unknown until worker finishes

    # SSE id: use frontend-provided job_id if given, else generate uuid.
    # RQ only allows letters/numbers/underscores/dashes — sanitize.
    import re
    raw_sse = job_id or uuid.uuid4().hex
    sse_job_id = re.sub(r'[^A-Za-z0-9_\-]', '-', str(raw_sse))[:128]

    # Enqueue the background extraction task.
    try:
        from jobs.queue import get_queue
        from jobs.tasks import run_v11_task
        rq_job = get_queue().enqueue(
            run_v11_task,
            kwargs={'job_id': sse_job_id, 'pdf_path': str(save_path)},
            job_id=sse_job_id,           # RQ job id matches SSE/stream id
            job_timeout=900,
            result_ttl=3600,
        )
    except Exception as e:
        try:
            save_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(500, f"Failed to enqueue V11 job: {e}")

    return {
        "status": "queued",
        "filename": file.filename,
        "mode": "v11",
        "job_id": None,  # worker will create — fetch via /api/extract-v11/status/{stream_id}.result.job_id
        "stream_id": sse_job_id,
        "queue_position": _queue_position(rq_job),
        "message": f"PDF queued. Stream live router events at /api/extract-v11/stream/{sse_job_id}",
    }


def _queue_position(rq_job) -> int:
    """Approx queue depth ahead of this job."""
    try:
        from jobs.queue import get_queue
        q = get_queue()
        return q.count
    except Exception:
        return 0


@app.get("/api/extract-v11/status/{stream_id}")
async def extract_v11_status(stream_id: str):
    """Poll RQ job status by stream_id (== rq job id).

    Returns:
      status: queued | started | finished | failed | unknown
      queue_position: int (>=0 if still queued, else -1)
      result: dict if finished
      error: string if failed
    """
    try:
        from rq.job import Job
        from jobs.queue import get_redis
        rq_job = Job.fetch(stream_id, connection=get_redis())
        try:
            pos = rq_job.get_position()
        except Exception:
            pos = None
        return {
            "status": rq_job.get_status(),
            "queue_position": pos if pos is not None else -1,
            "result": rq_job.result if rq_job.is_finished else None,
            "error": str(rq_job.exc_info) if rq_job.is_failed else None,
        }
    except Exception as e:
        return {"status": "unknown", "error": str(e)}


@app.get("/api/extract-v11/stream/{job_id}")
async def extract_v11_stream(job_id: str):
    """V11 — SSE live event stream for a router job."""
    import asyncio, json as _json
    from fastapi.responses import StreamingResponse

    try:
        from v11.event_bus import subscribe as _v11_subscribe
    except Exception as _e:
        _v11_subscribe = None
        _import_err = str(_e)
    else:
        _import_err = None

    _TERMINAL = {"DONE", "FAIL", "__CLOSE__"}
    _TIMEOUT_SECONDS = 600

    async def _event_gen():
        if _v11_subscribe is None:
            payload = _json.dumps({"error": f"v11.event_bus unavailable: {_import_err}"})
            yield f"event: FAIL\ndata: {payload}\n\n"
            return

        loop = asyncio.get_event_loop()
        deadline = loop.time() + _TIMEOUT_SECONDS

        try:
            source = _v11_subscribe(job_id)
        except Exception as e:
            payload = _json.dumps({"error": f"subscribe failed: {e}"})
            yield f"event: FAIL\ndata: {payload}\n\n"
            return

        # Support both async iterators and sync iterables/generators.
        if hasattr(source, "__aiter__"):
            aiter_obj = source.__aiter__()
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    yield f"event: __CLOSE__\ndata: {_json.dumps({'reason': 'timeout'})}\n\n"
                    return
                try:
                    event = await asyncio.wait_for(aiter_obj.__anext__(), timeout=remaining)
                except StopAsyncIteration:
                    return
                except asyncio.TimeoutError:
                    yield f"event: __CLOSE__\ndata: {_json.dumps({'reason': 'timeout'})}\n\n"
                    return
                except Exception as e:
                    yield f"event: FAIL\ndata: {_json.dumps({'error': str(e)})}\n\n"
                    return

                event_type, event_data = _split_event(event)
                yield f"event: {event_type}\ndata: {_json.dumps(event_data, default=str)}\n\n"
                if event_type in _TERMINAL:
                    return
        else:
            it = iter(source)
            while True:
                if loop.time() >= deadline:
                    yield f"event: __CLOSE__\ndata: {_json.dumps({'reason': 'timeout'})}\n\n"
                    return
                try:
                    event = await asyncio.to_thread(next, it, _SENTINEL)
                except Exception as e:
                    yield f"event: FAIL\ndata: {_json.dumps({'error': str(e)})}\n\n"
                    return
                if event is _SENTINEL:
                    return
                event_type, event_data = _split_event(event)
                yield f"event: {event_type}\ndata: {_json.dumps(event_data, default=str)}\n\n"
                if event_type in _TERMINAL:
                    return

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(_event_gen(), media_type="text/event-stream", headers=headers)


_SENTINEL = object()


def _split_event(event):
    """Normalize an event into (event_type, event_data)."""
    if isinstance(event, dict):
        et = event.get("type") or event.get("event") or "message"
        data = event.get("data", {k: v for k, v in event.items() if k not in ("type", "event")})
        return str(et), data
    if isinstance(event, (list, tuple)) and len(event) == 2:
        return str(event[0]), event[1]
    return "message", event


@app.get("/api/health")
async def health_check():
    """Health check with system stats."""
    import os
    stats = database.get_stats()

    # DB size
    db_path = database.DB_PATH
    db_size_mb = os.path.getsize(db_path) / (1024 * 1024) if os.path.exists(db_path) else 0

    return {
        "status": "ok",
        "version": "3.0.0",
        "pipeline": "LLM Assembler + Verifier",
        "database": {
            "total_jobs": stats["total_jobs"],
            "completed_jobs": stats["completed_jobs"],
            "size_mb": round(db_size_mb, 2),
        },
    }


# --- Serve Frontend Static Files (SPA fallback) ---
# In Docker: /app/frontend-build (copied during build)
# In dev: ../frontend/build (after npm run build)
FRONTEND_BUILD = Path(__file__).parent / "frontend-build"
if not FRONTEND_BUILD.exists():
    FRONTEND_BUILD = Path(__file__).parent.parent / "frontend" / "build"

if FRONTEND_BUILD.exists():
    from starlette.requests import Request as StarletteRequest
    from starlette.responses import Response as StarletteResponse

    # Mount /_app for JS/CSS bundles
    _app_dir = FRONTEND_BUILD / "_app"
    if _app_dir.exists():
        app.mount("/_app", StaticFiles(directory=str(_app_dir)), name="static-app")

    @app.middleware("http")
    async def spa_middleware(request: StarletteRequest, call_next):
        """SPA fallback: serve index.html for non-API GET 404s."""
        response = await call_next(request)
        path = request.url.path

        # Let API, docs, static assets, and redirects through
        if (path.startswith("/api")
            or path.startswith("/docs")
            or path.startswith("/openapi")
            or path.startswith("/_app")
            or response.status_code < 400):
            return response

        # For GET 404s on non-API paths, serve SPA
        if request.method == "GET" and response.status_code == 404:
            file_path = FRONTEND_BUILD / path.lstrip("/")
            if file_path.is_file():
                return FileResponse(file_path)
            return FileResponse(FRONTEND_BUILD / "index.html")

        return response
else:
    @app.get("/")
    async def root():
        return {"message": "RO-ED AI Agent API", "docs": "/docs", "health": "/api/health"}
