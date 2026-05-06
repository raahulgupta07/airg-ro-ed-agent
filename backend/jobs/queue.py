"""RQ queue setup + enqueue helpers."""
import os
from redis import Redis
from rq import Queue

REDIS_URL = os.environ.get('REDIS_URL', 'redis://redis:6379/0')

_redis = None
_queue = None


def get_redis():
    global _redis
    if _redis is None:
        _redis = Redis.from_url(REDIS_URL)
    return _redis


def get_queue():
    global _queue
    if _queue is None:
        _queue = Queue('ro_ed_jobs', connection=get_redis(), default_timeout=900)
    return _queue
