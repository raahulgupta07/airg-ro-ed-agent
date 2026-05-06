#!/usr/bin/env python3
"""Real-time cost tracker for API calls.

Records token usage + cost from each OpenRouter call.
Prefers OpenRouter's reported `usage.cost` (real billed amount, includes markup).
Falls back to local pricing table if OpenRouter omits cost.
"""

import threading

# OpenRouter pricing per MILLION tokens (fallback only; OpenRouter usage.cost is preferred)
MODEL_PRICING = {
    "google/gemini-3.1-flash-lite-preview": {"input": 0.25, "output": 1.50},
    "google/gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "google/gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
    "google/gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "google/gemini-3.1-pro-preview": {"input": 1.25, "output": 10.00},
    "google/gemini-pro-latest": {"input": 1.25, "output": 10.00},
    "~google/gemini-pro-latest": {"input": 1.25, "output": 10.00},
    "anthropic/claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "anthropic/claude-haiku-4-5": {"input": 0.80, "output": 4.00},
    "anthropic/claude-opus-4.7": {"input": 15.00, "output": 75.00},
}

DEFAULT_PRICING = {"input": 0.50, "output": 3.00}

_lock = threading.Lock()
_costs = {}      # step_name -> aggregated {input_tokens, output_tokens, cost, calls}
_entries = []    # per-call log [{step, model, input_tokens, output_tokens, cost, source}]
_total_cost = 0.0


def reset():
    """Reset all costs for a new pipeline run."""
    global _costs, _total_cost, _entries
    with _lock:
        _costs = {}
        _entries = []
        _total_cost = 0.0


def record(step_name: str, response_json: dict, model: str = ""):
    """Record cost from an OpenRouter API response.

    Prefers OpenRouter's `usage.cost` (real billed). Falls back to local pricing.
    Logs both per-step aggregate and per-call entry."""
    global _total_cost
    usage = response_json.get("usage", {}) or {}
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)

    if not model:
        model = response_json.get("model", "") or ""

    # Prefer OpenRouter's reported cost (real billed)
    or_cost = usage.get("cost") or usage.get("total_cost")
    if or_cost is not None:
        try:
            cost = float(or_cost)
            source = "openrouter"
        except Exception:
            or_cost = None
    if or_cost is None:
        pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
        cost = (input_tokens * pricing["input"] / 1_000_000) + (output_tokens * pricing["output"] / 1_000_000)
        source = "estimated"

    entry = {
        "step": step_name,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": cost,
        "source": source,
    }

    with _lock:
        if step_name not in _costs:
            _costs[step_name] = {"input_tokens": 0, "output_tokens": 0,
                                  "cost": 0.0, "calls": 0, "model": model}
        _costs[step_name]["input_tokens"] += input_tokens
        _costs[step_name]["output_tokens"] += output_tokens
        _costs[step_name]["cost"] += cost
        _costs[step_name]["calls"] += 1
        _entries.append(entry)
        _total_cost += cost


def get_step_cost(step_name: str) -> dict:
    """Get cost info for a step."""
    with _lock:
        return _costs.get(step_name, {"input_tokens": 0, "output_tokens": 0,
                                        "cost": 0.0, "calls": 0}).copy()


def get_total_cost() -> float:
    """Get total accumulated cost."""
    with _lock:
        return round(_total_cost, 6)


def get_total_tokens() -> dict:
    """Total input + output tokens across all calls."""
    with _lock:
        ti = sum(s["input_tokens"] for s in _costs.values())
        to = sum(s["output_tokens"] for s in _costs.values())
        return {"input_tokens": ti, "output_tokens": to, "total": ti + to}


def get_entries() -> list:
    """Per-call entries (for cost_breakdown response)."""
    with _lock:
        return [dict(e) for e in _entries]


def get_all() -> dict:
    """Aggregated steps + total + per-call entries."""
    with _lock:
        return {"steps": dict(_costs),
                "total": round(_total_cost, 6),
                "entries": [dict(e) for e in _entries],
                "tokens": {
                    "input": sum(s["input_tokens"] for s in _costs.values()),
                    "output": sum(s["output_tokens"] for s in _costs.values()),
                }}
