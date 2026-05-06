"""RQ worker entrypoint.

Run via: python worker.py

Listens on the 'ro_ed_jobs' queue and dispatches V11 extraction tasks.
"""
import os
import sys

# Ensure the backend package root is importable when invoked from other CWDs
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
# Also support the common docker layout where backend is mounted at /app
if os.path.isdir('/app') and '/app' not in sys.path:
    sys.path.insert(0, '/app')

# -----------------------------------------------------------------------------
# Sentry (optional — only initialized if SENTRY_DSN is set). Must come before
# RQ imports so the RqIntegration can patch worker job dispatch.
# -----------------------------------------------------------------------------
_DSN = os.environ.get("SENTRY_DSN", "").strip()
if _DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.rq import RqIntegration
        sentry_sdk.init(
            dsn=_DSN,
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
            release=os.environ.get("SENTRY_RELEASE", "ro-ed-worker@dev"),
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            send_default_pii=False,
            integrations=[RqIntegration()],
        )
        print(f"[sentry-worker] enabled — env={os.environ.get('SENTRY_ENVIRONMENT', 'production')}")
    except Exception as e:
        print(f"[sentry-worker] init failed: {e}")

from rq import Worker
from jobs.queue import get_redis, get_queue


if __name__ == '__main__':
    queue = get_queue()
    worker = Worker([queue], connection=get_redis())
    worker.work(with_scheduler=True)
