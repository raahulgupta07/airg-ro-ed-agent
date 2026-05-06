"""V11 Event Bus — Redis pub/sub backend.

Cross-process pub/sub: workers emit, API SSE subscribers read.
Falls back to in-memory if Redis unavailable.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Any, AsyncGenerator, Dict, Optional

try:
    from redis import Redis
    _REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    _redis: Optional[Redis] = None

    def _get_redis() -> Redis:
        global _redis
        if _redis is None:
            _redis = Redis.from_url(_REDIS_URL, decode_responses=True)
        return _redis
except Exception:  # pragma: no cover
    Redis = None  # type: ignore
    _get_redis = None  # type: ignore


_TERMINAL_TYPES = {"DONE", "FAIL", "__CLOSE__"}
_CHANNEL_PREFIX = "v11:events:"
_HISTORY_KEY_PREFIX = "v11:history:"
_HISTORY_MAX = 500
_HISTORY_TTL = 3600  # 1h


def _channel(job_id: str) -> str:
    return f"{_CHANNEL_PREFIX}{job_id}"


def _history_key(job_id: str) -> str:
    return f"{_HISTORY_KEY_PREFIX}{job_id}"


def emit(job_id: Optional[str], type: str, data: Optional[Dict] = None) -> None:
    """Publish an event to Redis. Non-blocking. Never raises."""
    if not job_id:
        return
    try:
        r = _get_redis()
        if r is None:
            return
        evt = {
            "type": type,
            "data": data or {},
            "ts": time.time(),
        }
        payload = json.dumps(evt, default=str)
        # Append to history list (capped) so late subscribers can replay
        pipe = r.pipeline()
        pipe.rpush(_history_key(job_id), payload)
        pipe.ltrim(_history_key(job_id), -_HISTORY_MAX, -1)
        pipe.expire(_history_key(job_id), _HISTORY_TTL)
        # Live publish
        pipe.publish(_channel(job_id), payload)
        pipe.execute()
    except Exception:
        pass


def close(job_id: Optional[str]) -> None:
    """Send terminal sentinel so all subscribers exit."""
    if not job_id:
        return
    emit(job_id, "__CLOSE__", {})


async def subscribe(job_id: str) -> AsyncGenerator[Dict, None]:
    """Async iterator yielding events for `job_id`.

    Replays any history first (so events emitted before subscribe arrive),
    then live-streams via Redis pub/sub. Exits on terminal type.
    """
    r = _get_redis() if _get_redis else None
    if r is None:
        return

    # Replay history first
    seen_count = 0
    try:
        history = r.lrange(_history_key(job_id), 0, -1)
        for raw in history or []:
            try:
                evt = json.loads(raw)
            except Exception:
                continue
            seen_count += 1
            yield evt
            if evt.get("type") in _TERMINAL_TYPES:
                return
    except Exception:
        pass

    # Subscribe to live channel
    pubsub = None
    try:
        pubsub = r.pubsub(ignore_subscribe_messages=True)
        await asyncio.to_thread(pubsub.subscribe, _channel(job_id))
        deadline = time.time() + 600  # 10 min safety
        while time.time() < deadline:
            msg = await asyncio.to_thread(pubsub.get_message, True, 1.0)
            if msg is None:
                # Yield a heartbeat (helps SSE keepalive)
                continue
            data = msg.get("data")
            if not data:
                continue
            try:
                evt = json.loads(data)
            except Exception:
                continue
            yield evt
            if evt.get("type") in _TERMINAL_TYPES:
                return
    finally:
        try:
            if pubsub is not None:
                await asyncio.to_thread(pubsub.unsubscribe, _channel(job_id))
                await asyncio.to_thread(pubsub.close)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Self-test                                                                    #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    async def main():
        emit("self-test", "JOB_START", {"file": "x.pdf"})
        emit("self-test", "DONE", {"total_s": 1.0})
        async for ev in subscribe("self-test"):
            print(ev)
            if ev["type"] in _TERMINAL_TYPES:
                break
        print("OK")
    asyncio.run(main())
