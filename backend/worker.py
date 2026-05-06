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

from rq import Worker
from jobs.queue import get_redis, get_queue


if __name__ == '__main__':
    queue = get_queue()
    worker = Worker([queue], connection=get_redis())
    worker.work(with_scheduler=True)
