"""Fleet store — persist extractions as DATA and run saved REPORTS over them.

No LLM, no fitz. Pure stdlib. Each extraction result is written once as JSON;
reports are saved DEFINITIONS (recipes) evaluated live, so new documents
automatically flow into existing reports.

Layout (all under ROVER_STORE_DIR, default /app/data/rover_store):
  documents/{doc_id}.json  — one extraction result per file
  reports/{name}.json      — one report definition per file

Grains:
  overall  — one row per document (header values + doc metadata)
  product  — one row per line item across all docs
  joined   — each product row merged with its document's header values
"""
import os
import json
import re

STORE_DIR = os.environ.get("ROVER_STORE_DIR", "/app/data/rover_store")
DOCS_DIR = os.path.join(STORE_DIR, "documents")
REPORTS_DIR = os.path.join(STORE_DIR, "reports")

_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _sanitize(raw: str) -> str:
    """Reduce an arbitrary string to a filesystem-safe [A-Za-z0-9_-] token."""
    return _ID_RE.sub("_", str(raw))


def _doc_id(result: dict) -> str:
    """Stable id for a result: declaration_no if present, else the pdf stem."""
    values = result.get("values") or {}
    decl = values.get("declaration_no")
    if decl:
        return _sanitize(decl)
    pdf = str(result.get("pdf", "") or "")
    stem = os.path.basename(pdf)
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    return _sanitize(stem)


def _read_json(path: str):
    """Load JSON, returning None on any read/parse error (fail-safe)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #
def save_document(result: dict) -> str:
    """Persist one extraction result to documents/{doc_id}.json; return doc_id."""
    os.makedirs(DOCS_DIR, exist_ok=True)
    doc_id = _doc_id(result)
    payload = dict(result)
    payload["_stored_id"] = doc_id
    path = os.path.join(DOCS_DIR, doc_id + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return doc_id


def load_document(doc_id: str):
    """Load a single stored document by id, or None if missing/unreadable."""
    return _read_json(os.path.join(DOCS_DIR, _sanitize(doc_id) + ".json"))


def list_document_ids() -> list:
    """Return the ids of all stored documents (sorted, best-effort)."""
    try:
        names = os.listdir(DOCS_DIR)
    except Exception:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json"))


def all_documents() -> list:
    """Load every stored document, skipping any that fail to parse."""
    docs = []
    for doc_id in list_document_ids():
        doc = load_document(doc_id)
        if doc is not None:
            docs.append(doc)
    return docs


# --------------------------------------------------------------------------- #
# row projections (grains)
# --------------------------------------------------------------------------- #
def overall_rows() -> list:
    """One flat row per document: header values + doc_id/pdf/needs_review."""
    rows = []
    for doc in all_documents():
        row = dict(doc.get("values") or {})
        row["doc_id"] = doc.get("_stored_id") or _doc_id(doc)
        row["pdf"] = doc.get("pdf")
        row["needs_review"] = doc.get("needs_review")
        rows.append(row)
    return rows


def product_rows() -> list:
    """One flat row per line item across all documents: item fields + doc_id."""
    rows = []
    for doc in all_documents():
        doc_id = doc.get("_stored_id") or _doc_id(doc)
        for item in doc.get("items") or []:
            row = dict(item)
            row["doc_id"] = doc_id
            rows.append(row)
    return rows


def joined_rows() -> list:
    """Each product row merged with its document's header values.

    On a key clash the item field wins. Documents with zero items still
    contribute one header-only row.
    """
    rows = []
    for doc in all_documents():
        doc_id = doc.get("_stored_id") or _doc_id(doc)
        header = dict(doc.get("values") or {})
        items = doc.get("items") or []
        if not items:
            row = dict(header)
            row["doc_id"] = doc_id
            rows.append(row)
            continue
        for item in items:
            row = dict(header)
            row.update(item)  # item field wins on clash
            row["doc_id"] = doc_id
            rows.append(row)
    return rows


_GRAINS = {
    "overall": overall_rows,
    "product": product_rows,
    "joined": joined_rows,
}


# --------------------------------------------------------------------------- #
# reports
# --------------------------------------------------------------------------- #
def save_report(definition: dict) -> str:
    """Persist a report definition to reports/{name}.json; return its name."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    name = definition["name"]
    path = os.path.join(REPORTS_DIR, _sanitize(name) + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(definition, fh, ensure_ascii=False, indent=2)
    return name


def load_report(name: str):
    """Load a saved report definition by name, or None if missing/unreadable."""
    return _read_json(os.path.join(REPORTS_DIR, _sanitize(name) + ".json"))


def list_reports() -> list:
    """Return the names of all saved reports (sorted, best-effort)."""
    try:
        names = os.listdir(REPORTS_DIR)
    except Exception:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json"))


def _passes(row: dict, flt: dict) -> bool:
    """Evaluate one filter against a row (missing field fails the filter)."""
    field = flt.get("field")
    op = flt.get("op")
    target = flt.get("value")
    if field not in row:
        return False
    actual = row[field]
    try:
        if op == "eq":
            return actual == target
        if op == "ne":
            return actual != target
        if op == "gt":
            return actual is not None and actual > target
        if op == "lt":
            return actual is not None and actual < target
        if op == "contains":
            return target is not None and str(target) in str(actual)
    except TypeError:
        return False
    return False


def run_report(name: str) -> list:
    """Evaluate a saved report live and return the resulting list of rows.

    Picks rows by grain, applies all filters (AND), projects the requested
    columns (all keys if none given), then stably sorts by ``sort`` if set.
    """
    definition = load_report(name)
    if definition is None:
        return []

    grain = definition.get("grain", "overall")
    rows = _GRAINS.get(grain, overall_rows)()

    filters = definition.get("filters") or []
    if filters:
        rows = [r for r in rows if all(_passes(r, f) for f in filters)]

    columns = definition.get("columns") or []
    if columns:
        rows = [{c: r.get(c) for c in columns} for r in rows]

    sort = definition.get("sort")
    if sort:
        rows = sorted(
            rows,
            key=lambda r: (r.get(sort) is None, _sort_key(r.get(sort))),
        )
    return rows


def _sort_key(value):
    """None-safe, type-safe sort key: keep native order, fall back to str."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return value
    return str(value)


# --------------------------------------------------------------------------- #
# backend dispatcher
# --------------------------------------------------------------------------- #
# Callers use `from rover import store; store.save_document(...)`. The file-based
# implementations above are the default. When ROVER_STORE=pg, rebind the public
# names to the Postgres-backed store so callers switch transparently, with no
# change to the file code above. A PG import failure falls back to files.
_BACKEND = os.environ.get("ROVER_STORE", "files")

if _BACKEND == "pg":
    try:
        from . import store_pg as _pg

        save_document = _pg.save_document
        load_document = _pg.load_document
        list_document_ids = _pg.list_document_ids
        all_documents = _pg.all_documents
        overall_rows = _pg.overall_rows
        product_rows = _pg.product_rows
        joined_rows = _pg.joined_rows
        save_report = _pg.save_report
        load_report = _pg.load_report
        list_reports = _pg.list_reports
        run_report = _pg.run_report
        # Keep the grain map consistent with the rebound row functions.
        _GRAINS = {
            "overall": overall_rows,
            "product": product_rows,
            "joined": joined_rows,
        }
    except Exception as _exc:  # fail-safe — fall back to the file store above
        print(f"[rover.store] ROVER_STORE=pg import failed, using files: {_exc}")
