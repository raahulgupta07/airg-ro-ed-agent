"""Usage analytics endpoints — total cost, tokens, doc-type breakdown."""
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
import database

router = APIRouter()


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
