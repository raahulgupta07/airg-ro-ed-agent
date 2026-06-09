"""Background job tasks (executed by RQ worker).

Each task runs in the worker process — it must import its own dependencies and
must update the DB so the API can poll job status.
"""


def run_v11_task(job_id: str, pdf_path: str, engine: str = "auto") -> dict:
    """Worker entry: run V11 pipeline, update DB on failure.

    `engine` selects the typed-page engine: "auto" (PRESTO_ENABLED flag),
    "presto" (force V12 fast-path), or "classic" (force V7 Veritas).

    On success, the V11 workflow itself writes results to the DB (per
    existing behavior). On failure we mark the DB job FAILED so polling
    callers see the terminal state.
    """
    import database
    try:
        from v11.workflow import run as run_v11
        result = run_v11(pdf_path, job_id=job_id, engine=engine)
        return result
    except Exception:
        try:
            database.update_job_status(job_id, 'FAILED')
        except Exception:
            pass
        raise
    finally:
        # Cleanup PDF only on FAIL is handled in workflow — keep on success for review
        pass
