"""
V11 In-Memory Event Bus
=======================

Pure stdlib (asyncio + threading) pub/sub bus keyed by `job_id`.

Design notes
------------
* `emit()` is non-blocking and safe from background sync threads. V11 spawns
  ThreadPoolExecutor children that need to push events back to an asyncio
  consumer running in the main thread. We capture the event loop on first
  contact with `subscribe()` and use `asyncio.run_coroutine_threadsafe` from
  foreign threads. If no loop is captured yet (or we are already on the loop
  thread), we fall back to `Queue.put_nowait`.
* `emit()` MUST never raise. All exceptions are swallowed.
* Queue cap is 500 events. When full we drop the oldest by popping one item
  before pushing the new one (best-effort, no hard guarantee under heavy
  contention but good enough for telemetry-style streams).
* TTL cleanup task drops queues idle > 1h with no subscriber. Spawned lazily
  on first emit when a loop is known.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, AsyncGenerator, Dict, Optional


# --------------------------------------------------------------------------- #
# Internal state                                                              #
# --------------------------------------------------------------------------- #

_QUEUE_MAXSIZE = 500
_TTL_SECONDS = 3600          # drop queues idle > 1h
_TTL_SWEEP_INTERVAL = 300    # sweep every 5 min
_CLOSE_GRACE_SECONDS = 60    # keep queue alive 60s after close()

_lock = threading.RLock()
_queues: Dict[str, asyncio.Queue] = {}
_meta: Dict[str, Dict[str, Any]] = {}   # job_id -> {"last": ts, "subs": int, "closed_at": ts|None}
_loop: Optional[asyncio.AbstractEventLoop] = None
_cleanup_task_started = False


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _capture_loop() -> Optional[asyncio.AbstractEventLoop]:
    """Capture the running loop if we don't already have one."""
    global _loop
    if _loop is not None:
        return _loop
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        _loop = None
    return _loop


def _get_or_create_queue(job_id: str) -> asyncio.Queue:
    with _lock:
        q = _queues.get(job_id)
        if q is None:
            q = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
            _queues[job_id] = q
            _meta[job_id] = {"last": time.time(), "subs": 0, "closed_at": None}
        return q


def _put_with_drop(q: asyncio.Queue, event: Dict[str, Any]) -> None:
    """Put event, dropping oldest if full. Must be called on loop thread or
    via run_coroutine_threadsafe wrapping."""
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        try:
            _ = q.get_nowait()
        except Exception:
            pass
        try:
            q.put_nowait(event)
        except Exception:
            pass


async def _async_put(q: asyncio.Queue, event: Dict[str, Any]) -> None:
    _put_with_drop(q, event)


async def _ttl_sweeper() -> None:
    while True:
        try:
            await asyncio.sleep(_TTL_SWEEP_INTERVAL)
            now = time.time()
            with _lock:
                for jid in list(_queues.keys()):
                    m = _meta.get(jid, {})
                    closed_at = m.get("closed_at")
                    last = m.get("last", 0)
                    subs = m.get("subs", 0)
                    if closed_at is not None and (now - closed_at) > _CLOSE_GRACE_SECONDS:
                        _queues.pop(jid, None)
                        _meta.pop(jid, None)
                        continue
                    if subs == 0 and (now - last) > _TTL_SECONDS:
                        _queues.pop(jid, None)
                        _meta.pop(jid, None)
        except asyncio.CancelledError:
            return
        except Exception:
            # never die
            continue


def _maybe_start_cleanup() -> None:
    global _cleanup_task_started
    if _cleanup_task_started:
        return
    loop = _capture_loop()
    if loop is None:
        return
    try:
        if loop.is_running():
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_ttl_sweeper(), loop=loop))
            _cleanup_task_started = True
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #

def emit(job_id: str, type: str, data: dict) -> None:
    """Non-blocking, thread-safe event emission. Never raises."""
    try:
        event = {
            "type": type,
            "data": data if isinstance(data, dict) else {"value": data},
            "ts": time.time(),
        }
        q = _get_or_create_queue(job_id)
        with _lock:
            if job_id in _meta:
                _meta[job_id]["last"] = event["ts"]

        loop = _loop
        # Are we currently inside the captured loop's thread?
        on_loop_thread = False
        try:
            running = asyncio.get_running_loop()
            if loop is None:
                # Capture if a loop is actually running on this thread
                _capture_loop()
                loop = _loop
            on_loop_thread = (running is loop)
        except RuntimeError:
            on_loop_thread = False

        if loop is not None and not on_loop_thread:
            # Foreign thread -> schedule on loop
            try:
                asyncio.run_coroutine_threadsafe(_async_put(q, event), loop)
            except Exception:
                # Fallback best-effort
                try:
                    _put_with_drop(q, event)
                except Exception:
                    pass
        else:
            # On loop thread (or no loop yet) -> direct put
            _put_with_drop(q, event)

        _maybe_start_cleanup()
    except Exception:
        # emit must NEVER raise
        return


async def subscribe(job_id: str) -> AsyncGenerator[Dict[str, Any], None]:
    """Async generator yielding events for `job_id`. Auto-creates queue."""
    _capture_loop()
    q = _get_or_create_queue(job_id)
    with _lock:
        if job_id in _meta:
            _meta[job_id]["subs"] = _meta[job_id].get("subs", 0) + 1
    _maybe_start_cleanup()
    try:
        while True:
            event = await q.get()
            if isinstance(event, dict) and event.get("type") == "__CLOSE__":
                yield event
                return
            yield event
    finally:
        with _lock:
            if job_id in _meta:
                _meta[job_id]["subs"] = max(0, _meta[job_id].get("subs", 1) - 1)


def close(job_id: str) -> None:
    """Push a close sentinel; queue is reaped after grace period."""
    try:
        q = _get_or_create_queue(job_id)
        sentinel = {"type": "__CLOSE__", "data": {}, "ts": time.time()}

        loop = _loop
        on_loop_thread = False
        try:
            running = asyncio.get_running_loop()
            on_loop_thread = (running is loop)
        except RuntimeError:
            on_loop_thread = False

        if loop is not None and not on_loop_thread:
            try:
                asyncio.run_coroutine_threadsafe(_async_put(q, sentinel), loop)
            except Exception:
                _put_with_drop(q, sentinel)
        else:
            _put_with_drop(q, sentinel)

        with _lock:
            if job_id in _meta:
                _meta[job_id]["closed_at"] = time.time()
    except Exception:
        return


# --------------------------------------------------------------------------- #
# Self-test                                                                   #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    async def _main() -> None:
        job = "test-job-1"

        # Emit from a background thread to exercise the threadsafe path.
        def _bg() -> None:
            emit(job, "step", {"i": 1, "msg": "hello"})
            emit(job, "step", {"i": 2, "msg": "world"})
            emit(job, "done", {"i": 3, "msg": "bye"})
            close(job)

        # Make sure the loop is captured before bg thread emits.
        _capture_loop()
        # Subscribe first, then fire thread.
        seen = []

        async def _consume():
            async for ev in subscribe(job):
                if ev["type"] == "__CLOSE__":
                    break
                seen.append(ev)

        consumer = asyncio.create_task(_consume())
        await asyncio.sleep(0.05)  # let subscribe enter loop
        threading.Thread(target=_bg, daemon=True).start()
        await asyncio.wait_for(consumer, timeout=2.0)

        print(f"received {len(seen)} events:")
        for ev in seen:
            print(f"  - type={ev['type']} data={ev['data']} ts={ev['ts']:.3f}")

        assert len(seen) == 3, f"expected 3 events, got {len(seen)}"
        assert [e["data"]["i"] for e in seen] == [1, 2, 3]
        print("OK: event_bus self-test passed.")

    asyncio.run(_main())
