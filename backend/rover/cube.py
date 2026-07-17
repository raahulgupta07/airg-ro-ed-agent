"""ROVER cube/report engine — pivot & aggregate the stored extractions.

No LLM, no fitz. Pure stdlib over the row projections in ``store`` (overall /
product / joined grains). A *cube* is a saved SPEC (dimensions + measures +
filters + sort); running it groups the live rows and aggregates, so new
documents automatically flow into existing cubes.

SPEC shape:
  { "name": str, "grain": "document"|"product"|"joined",
    "dimensions": [field, ...],
    "measures": [ {"id", "field"|null, "agg": sum|avg|count|min|max, "label"}, ... ],
    "filters": [ {"field", "op": eq|ne|gt|lt|contains, "value"}, ... ],
    "sort": "field" | "-field" | measure-id | None }

RUN RESULT shape:
  { "dimensions": [...], "measures": [{"id","label"}, ...],
    "rows": [ {<dim>: val, <measure_id>: number}, ... ],
    "totals": {<measure_id>: number}, "group_count": int, "row_count": int }
"""
import os
import io
import csv
import json
import re

from . import store

_ID_RE = re.compile(r"[^A-Za-z0-9_-]")

# grain aliases: the spec grain "document" maps to store.overall_rows
_GRAIN_FUNCS = {
    "document": "overall_rows",
    "overall": "overall_rows",
    "product": "product_rows",
    "joined": "joined_rows",
}


# --------------------------------------------------------------------------- #
# data access
# --------------------------------------------------------------------------- #
def _rows(grain: str) -> list:
    """Return the live rows for a grain (default document/overall). Fail-safe []."""
    try:
        fname = _GRAIN_FUNCS.get(grain or "document", "overall_rows")
        fn = getattr(store, fname, store.overall_rows)
        rows = fn()
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _num(v):
    """Coerce a value to float (stripping commas), or None if not numeric."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# field discovery
# --------------------------------------------------------------------------- #
def discover_fields(grain: str) -> dict:
    """Sample the rows for a grain and split fields into dimensions & measures.

    A field is a *measure* when its value is numeric in >50% of the rows where
    it is present (non-null). Everything else is a *dimension*. 'doc_id' is
    always kept as a dimension; the internal '_stored_id' key is dropped.
    """
    try:
        rows = _rows(grain)
        keys = set()
        for r in rows:
            if isinstance(r, dict):
                keys.update(r.keys())
        keys.discard("_stored_id")

        dimensions = sorted(keys)

        measures = []
        for k in keys:
            if k == "doc_id":
                continue
            non_null = 0
            numeric = 0
            for r in rows:
                if not isinstance(r, dict) or k not in r:
                    continue
                val = r.get(k)
                if val is None or val == "":
                    continue
                non_null += 1
                if _num(val) is not None:
                    numeric += 1
            if non_null > 0 and (numeric / non_null) > 0.5:
                measures.append(k)

        return {"dimensions": dimensions, "measures": sorted(measures)}
    except Exception as e:
        return {"dimensions": [], "measures": [], "error": str(e)}


# --------------------------------------------------------------------------- #
# filtering
# --------------------------------------------------------------------------- #
def _passes(row: dict, flt: dict) -> bool:
    """Evaluate one filter against a row.

    A missing field fails a positive op (eq/gt/lt/contains) but passes ``ne``.
    ``contains`` is a case-insensitive substring test; gt/lt compare numerically.
    """
    try:
        field = flt.get("field")
        op = flt.get("op")
        target = flt.get("value")
        present = isinstance(row, dict) and field in row
        actual = row.get(field) if present else None

        if op == "eq":
            return present and actual == target
        if op == "ne":
            return actual != target
        if op == "gt":
            a, b = _num(actual), _num(target)
            return a is not None and b is not None and a > b
        if op == "lt":
            a, b = _num(actual), _num(target)
            return a is not None and b is not None and a < b
        if op == "contains":
            if not present or target is None:
                return False
            return str(target).lower() in str(actual).lower()
        return False
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #
def _aggregate(group: list, measure: dict):
    """Aggregate one measure over a list of rows → a number."""
    agg = (measure.get("agg") or "count").lower()
    field = measure.get("field")

    if agg == "count" or not field:
        return len(group)

    nums = []
    for r in group:
        n = _num(r.get(field)) if isinstance(r, dict) else None
        if n is not None:
            nums.append(n)

    if not nums:
        return 0
    if agg == "sum":
        return sum(nums)
    if agg == "avg":
        return sum(nums) / len(nums)
    if agg == "min":
        return min(nums)
    if agg == "max":
        return max(nums)
    return len(group)


def _sort_key(value):
    """None-safe, type-safe sort key."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return value
    return str(value)


def run_cube(spec: dict) -> dict:
    """Run a cube spec against the live rows and return the RUN RESULT shape."""
    try:
        spec = spec or {}
        grain = spec.get("grain", "document")
        dimensions = list(spec.get("dimensions") or [])
        measures = list(spec.get("measures") or [])
        filters = list(spec.get("filters") or [])
        sort = spec.get("sort")

        rows = [r for r in _rows(grain)
                if isinstance(r, dict) and all(_passes(r, f) for f in filters)]

        # group by the dimension tuple (empty dimensions → one grand group)
        groups = {}
        order = []
        for r in rows:
            key = tuple(r.get(d) for d in dimensions)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(r)

        result_rows = []
        for key in order:
            group = groups[key]
            out = {}
            for i, d in enumerate(dimensions):
                out[d] = key[i]
            for m in measures:
                mid = m.get("id")
                if mid is None:
                    continue
                out[mid] = _aggregate(group, m)
            result_rows.append(out)

        # totals per measure over ALL filtered rows
        totals = {}
        for m in measures:
            mid = m.get("id")
            if mid is None:
                continue
            agg = (m.get("agg") or "count").lower()
            if agg == "avg":
                field = m.get("field")
                nums = []
                if field:
                    for r in rows:
                        n = _num(r.get(field))
                        if n is not None:
                            nums.append(n)
                totals[mid] = (sum(nums) / len(nums)) if nums else 0
            else:
                totals[mid] = _aggregate(rows, m)

        # sort
        if sort:
            desc = sort.startswith("-")
            key_name = sort[1:] if desc else sort
            result_rows.sort(key=lambda row: _sort_key(row.get(key_name)),
                             reverse=desc)

        return {
            "dimensions": dimensions,
            "measures": [{"id": m.get("id"), "label": m.get("label") or m.get("id")}
                         for m in measures if m.get("id") is not None],
            "rows": result_rows,
            "totals": totals,
            "group_count": len(result_rows),
            "row_count": len(rows),
        }
    except Exception as e:
        return {
            "dimensions": spec.get("dimensions", []) if isinstance(spec, dict) else [],
            "measures": [],
            "rows": [],
            "totals": {},
            "group_count": 0,
            "row_count": 0,
            "error": str(e),
        }


def to_csv(result: dict) -> str:
    """Render a RUN RESULT as CSV: dimensions + measure labels header, one row each."""
    try:
        result = result or {}
        dims = list(result.get("dimensions") or [])
        measures = list(result.get("measures") or [])
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(dims + [m.get("label") or m.get("id") for m in measures])
        for row in result.get("rows") or []:
            line = [row.get(d) for d in dims]
            line += [row.get(m.get("id")) for m in measures]
            writer.writerow(line)
        return buf.getvalue()
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# spec persistence
# --------------------------------------------------------------------------- #
def _cubes_dir() -> str:
    base = os.environ.get("ROVER_STORE_DIR", "/app/data/rover_store")
    return os.path.join(base, "cubes")


def _sanitize(raw: str) -> str:
    return _ID_RE.sub("_", str(raw))


def save_cube(spec: dict) -> str:
    """Persist a cube spec to cubes/<name>.json; return the sanitized name."""
    try:
        cubes_dir = _cubes_dir()
        os.makedirs(cubes_dir, exist_ok=True)
        name = _sanitize((spec or {}).get("name") or "cube")
        path = os.path.join(cubes_dir, name + ".json")
        payload = dict(spec or {})
        payload["name"] = name
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return name
    except Exception:
        return ""


def list_cubes() -> list:
    """Load every saved cube spec, sorted by name. Fail-safe []."""
    try:
        cubes_dir = _cubes_dir()
        names = os.listdir(cubes_dir)
    except Exception:
        return []
    specs = []
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(cubes_dir, n), "r", encoding="utf-8") as fh:
                specs.append(json.load(fh))
        except Exception:
            continue
    return sorted(specs, key=lambda s: str(s.get("name") or "") if isinstance(s, dict) else "")


def load_cube(name: str):
    """Load one saved cube spec by name, or None if missing/unreadable."""
    try:
        path = os.path.join(_cubes_dir(), _sanitize(name) + ".json")
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def delete_cube(name: str) -> bool:
    """Delete a saved cube spec. Return True if removed, False otherwise."""
    try:
        path = os.path.join(_cubes_dir(), _sanitize(name) + ".json")
        if os.path.exists(path):
            os.remove(path)
            return True
        return False
    except Exception:
        return False
