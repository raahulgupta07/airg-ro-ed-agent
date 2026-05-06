"""V10 PRO — Cost accumulator (global, lock-protected).

Each HTTP caller in v10_pro calls `record_cost(...)` after a successful
OpenRouter response. The workflow resets at start of run() and reads total
at the end.

Uses a global list + threading.Lock so child threads (e.g. multi_dpi_vote
ThreadPoolExecutor workers) are captured. NOT safe for concurrent /run()
calls in the same process — wrap workflow.run() in a per-request lock
upstream if needed.
"""
import threading
from typing import Dict, List

_lock = threading.Lock()
_entries: List[Dict] = []


def reset():
    global _entries
    with _lock:
        _entries = []


def record_cost(label: str, model: str, usage: Dict):
    """Append one entry to the global cost log."""
    cost = (usage or {}).get("cost") or (usage or {}).get("total_cost") or 0
    with _lock:
        _entries.append({
            "label": label,
            "model": model,
            "cost": float(cost or 0),
            "prompt_tokens": (usage or {}).get("prompt_tokens"),
            "completion_tokens": (usage or {}).get("completion_tokens"),
        })


def get_entries() -> List[Dict]:
    with _lock:
        return list(_entries)


def total() -> float:
    return round(sum(e.get("cost", 0) for e in get_entries()), 6)
