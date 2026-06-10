"""Usage analytics endpoints — total cost, tokens, doc-type breakdown."""
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Depends
import database
from middleware import require_admin

router = APIRouter()


@router.get("/overview")
async def usage_overview(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    current_user: dict = Depends(require_admin),
):
    """Admin usage dashboard for a date range: KPI totals, per-user breakdown,
    per-model breakdown, and a daily trend. All grouped in SQL over `jobs`."""
    where = ["1=1"]
    params: list = []
    if date_from:
        where.append("DATE(created_at) >= DATE(?)")
        params.append(date_from)
    if date_to:
        where.append("DATE(created_at) <= DATE(?)")
        params.append(date_to)
    w = " AND ".join(where)

    conn = database._connect()
    cur = conn.cursor()
    try:
        # ── KPI totals ──
        cur.execute(f"""
            SELECT COUNT(*)                       AS requests,
                   COALESCE(SUM(cost_usd), 0)     AS total_cost,
                   COALESCE(SUM(tokens_in), 0)    AS tokens_in,
                   COALESCE(SUM(tokens_out), 0)   AS tokens_out,
                   COALESCE(AVG(cost_usd), 0)     AS avg_cost,
                   COUNT(DISTINCT user_id)        AS users
            FROM jobs WHERE {w}
        """, tuple(params))
        k = cur.fetchone()
        kpis = {
            "requests": int(k[0] or 0),
            "total_cost": round(k[1] or 0, 4),
            "tokens_in": int(k[2] or 0),
            "tokens_out": int(k[3] or 0),
            "token_volume": int((k[2] or 0) + (k[3] or 0)),
            "avg_cost": round(k[4] or 0, 4),
            "users": int(k[5] or 0),
        }

        # ── Per-user ──
        cur.execute(f"""
            SELECT COALESCE(username, 'system')   AS username,
                   COUNT(*)                        AS requests,
                   COALESCE(SUM(tokens_in), 0)     AS tokens_in,
                   COALESCE(SUM(tokens_out), 0)    AS tokens_out,
                   COALESCE(SUM(cost_usd), 0)      AS cost,
                   MAX(created_at)                 AS last_active
            FROM jobs WHERE {w}
            GROUP BY COALESCE(username, 'system')
            ORDER BY cost DESC
        """, tuple(params))
        per_user = [
            {"username": r[0], "requests": int(r[1]),
             "tokens_in": int(r[2]), "tokens_out": int(r[3]),
             "tokens": int(r[2]) + int(r[3]),
             "cost": round(r[4] or 0, 4), "last_active": r[5]}
            for r in cur.fetchall()
        ]

        # ── Per-model ──
        cur.execute(f"""
            SELECT COALESCE(model_used, 'unknown') AS model,
                   COUNT(*)                         AS requests,
                   COALESCE(SUM(tokens_in), 0)      AS tokens_in,
                   COALESCE(SUM(tokens_out), 0)     AS tokens_out,
                   COALESCE(SUM(cost_usd), 0)       AS cost
            FROM jobs WHERE {w}
            GROUP BY COALESCE(model_used, 'unknown')
            ORDER BY cost DESC
        """, tuple(params))
        by_model = [
            {"model": r[0], "requests": int(r[1]),
             "tokens": int(r[2]) + int(r[3]),
             "cost": round(r[4] or 0, 4)}
            for r in cur.fetchall()
        ]

        # ── Daily trend ──
        cur.execute(f"""
            SELECT DATE(created_at)               AS day,
                   COUNT(*)                        AS requests,
                   COALESCE(SUM(cost_usd), 0)      AS cost,
                   COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0) AS tokens
            FROM jobs WHERE {w}
            GROUP BY DATE(created_at) ORDER BY day
        """, tuple(params))
        daily = [
            {"day": str(r[0])[:10], "requests": int(r[1]),
             "cost": round(r[2] or 0, 4), "tokens": int(r[3] or 0)}
            for r in cur.fetchall() if r[0]
        ]

        return {"kpis": kpis, "per_user": per_user,
                "by_model": by_model, "daily": daily}
    finally:
        conn.close()


@router.get("/summary")
async def usage_summary():
    """Grand totals: total cost, tokens, jobs, avg cost/doc."""
    conn = database._connect()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                COUNT(*) AS n_jobs,
                COALESCE(SUM(cost_usd), 0) AS total_cost,
                COALESCE(SUM(tokens_in), 0) AS total_tokens_in,
                COALESCE(SUM(tokens_out), 0) AS total_tokens_out,
                COALESCE(AVG(cost_usd), 0) AS avg_cost,
                COALESCE(AVG(processing_time_seconds), 0) AS avg_time
            FROM jobs
            WHERE status = 'COMPLETED'
        """)
        row = cur.fetchone()
        return {
            "n_jobs": row[0] or 0,
            "total_cost": round(row[1] or 0, 4),
            "total_tokens_in": int(row[2] or 0),
            "total_tokens_out": int(row[3] or 0),
            "total_tokens": int((row[2] or 0) + (row[3] or 0)),
            "avg_cost": round(row[4] or 0, 4),
            "avg_time_seconds": round(row[5] or 0, 1),
        }
    finally:
        conn.close()


@router.get("/per-doc")
async def usage_per_doc(limit: int = Query(100, ge=1, le=500),
                         offset: int = Query(0, ge=0)):
    """Paginated list of jobs with cost/tokens/type/mode."""
    conn = database._connect()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                job_id, pdf_name, created_at, completed_at,
                COALESCE(document_type, 'UNKNOWN') AS document_type,
                COALESCE(pipeline_mode, pipeline_version, 'unknown') AS pipeline_mode,
                COALESCE(total_pages, 0) AS pages,
                COALESCE(processing_time_seconds, 0) AS time_s,
                COALESCE(cost_usd, 0) AS cost,
                COALESCE(tokens_in, 0) AS tokens_in,
                COALESCE(tokens_out, 0) AS tokens_out,
                status
            FROM jobs
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))
        rows = cur.fetchall()
        return {
            "jobs": [
                {"job_id": r[0], "pdf_name": r[1],
                 "created_at": r[2], "completed_at": r[3],
                 "document_type": r[4], "pipeline_mode": r[5],
                 "pages": r[6], "time_seconds": round(r[7], 1),
                 "cost": round(r[8], 4),
                 "tokens_in": r[9], "tokens_out": r[10],
                 "status": r[11]}
                for r in rows
            ],
            "limit": limit, "offset": offset,
        }
    finally:
        conn.close()


@router.get("/by-type")
async def usage_by_type():
    """Aggregate cost by document_type."""
    conn = database._connect()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                COALESCE(document_type, 'UNKNOWN') AS dtype,
                COUNT(*) AS n,
                COALESCE(SUM(cost_usd), 0) AS total_cost,
                COALESCE(AVG(cost_usd), 0) AS avg_cost,
                COALESCE(SUM(tokens_in), 0) AS tokens_in,
                COALESCE(SUM(tokens_out), 0) AS tokens_out
            FROM jobs
            WHERE status = 'COMPLETED'
            GROUP BY dtype
            ORDER BY total_cost DESC
        """)
        rows = cur.fetchall()
        return {
            "by_type": [
                {"document_type": r[0], "n_jobs": r[1],
                 "total_cost": round(r[2], 4),
                 "avg_cost": round(r[3], 4),
                 "tokens_in": int(r[4]), "tokens_out": int(r[5])}
                for r in rows
            ],
        }
    finally:
        conn.close()


@router.get("/by-pipeline")
async def usage_by_pipeline():
    """Aggregate cost by pipeline_mode (v7/v10/v11)."""
    conn = database._connect()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                COALESCE(pipeline_mode, pipeline_version, 'unknown') AS pipe,
                COUNT(*) AS n,
                COALESCE(SUM(cost_usd), 0) AS total_cost,
                COALESCE(AVG(cost_usd), 0) AS avg_cost,
                COALESCE(SUM(tokens_in), 0) AS tokens_in,
                COALESCE(SUM(tokens_out), 0) AS tokens_out,
                COALESCE(AVG(processing_time_seconds), 0) AS avg_time
            FROM jobs
            WHERE status = 'COMPLETED'
            GROUP BY pipe
            ORDER BY total_cost DESC
        """)
        rows = cur.fetchall()
        return {
            "by_pipeline": [
                {"pipeline": r[0], "n_jobs": r[1],
                 "total_cost": round(r[2], 4),
                 "avg_cost": round(r[3], 4),
                 "tokens_in": int(r[4]), "tokens_out": int(r[5]),
                 "avg_time_seconds": round(r[6], 1)}
                for r in rows
            ],
        }
    finally:
        conn.close()
