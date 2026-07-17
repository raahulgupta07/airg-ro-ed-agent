"""Fleet store — PostgreSQL backend (mirror of rover/store.py).

Same public API as the file-based store, but persisted in three tables via the
app's pooled psycopg connection (database.get_conn()) so it inherits the same
DSN / QueuePool. Selected at runtime by ROVER_STORE=pg (see store.py dispatcher).

Tables:
  rover_documents(doc_id pk, pdf, header jsonb, items jsonb, needs_review,
                  suspect jsonb, reviewed, cost, created_at)
  rover_items(id bigserial pk, doc_id, no, hs_code, description, quantity,
              unit, value)
  rover_reports(name pk, grain, columns jsonb, filters jsonb, sort)

Everything is fail-safe: on any DB error a function logs via print and returns
None / [] / a best-effort value — it never raises out to the caller. This keeps
the store swappable behind the same contract as the stdlib file store.
"""
import json

# Reuse the exact id logic + filter/sort helpers from the file store so the two
# backends stay behaviourally identical. These are pure-Python, no DB.
from .store import _doc_id, _passes, _sort_key


# --------------------------------------------------------------------------- #
# connection + schema
# --------------------------------------------------------------------------- #
def _conn():
    """Pooled sqlite-compat connection. Imported lazily so importing this module
    (e.g. for py_compile) never requires psycopg/DB to be present."""
    import db_engine  # lazy: avoids import-time DB/psycopg dependency
    return db_engine.get_conn()


def ensure_tables():
    """CREATE TABLE IF NOT EXISTS for all three tables. Cheap + idempotent;
    called at the top of every public function so first use self-initializes."""
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rover_documents (
                doc_id       TEXT PRIMARY KEY,
                pdf          TEXT,
                header       JSONB,
                items        JSONB,
                needs_review BOOLEAN,
                suspect      JSONB,
                reviewed     BOOLEAN DEFAULT FALSE,
                cost         NUMERIC,
                raw          JSONB,
                created_at   TIMESTAMPTZ DEFAULT now()
            )
        """)
        # for tables created before these columns existed
        cur.execute("ALTER TABLE rover_documents ADD COLUMN IF NOT EXISTS raw JSONB")
        cur.execute("ALTER TABLE rover_documents ADD COLUMN IF NOT EXISTS job_id TEXT")
        cur.execute("ALTER TABLE rover_documents ADD COLUMN IF NOT EXISTS approved BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE rover_documents ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE rover_documents ADD COLUMN IF NOT EXISTS approved_by TEXT")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rover_items (
                id          BIGSERIAL PRIMARY KEY,
                doc_id      TEXT,
                no          INTEGER,
                hs_code     TEXT,
                description TEXT,
                quantity    NUMERIC,
                unit        TEXT,
                value       NUMERIC
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rover_reports (
                name    TEXT PRIMARY KEY,
                grain   TEXT,
                columns JSONB,
                filters JSONB,
                sort    TEXT
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_rover_items_doc ON rover_items(doc_id)"
        )
        conn.commit()
    except Exception as exc:  # fail-safe — never raise out
        print(f"[rover.store_pg.ensure_tables] {exc}")
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _dumps(value):
    """JSON-encode a value for a jsonb param (None stays None)."""
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return None


def _loads(value):
    """Decode a jsonb column. psycopg may return it already-parsed (dict/list)
    or as text — handle both."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return value


def _num(value):
    """Best-effort numeric coercion for the flat rover_items columns."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return None


def _int(value):
    n = _num(value)
    if n is None:
        return None
    try:
        return int(n)
    except Exception:
        return None


def _item_get(item, *keys):
    """First non-empty value among keys (mirrors the flexible extraction dicts)."""
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return v
    return None


def _gen_job_id(doc_id: str = "") -> str:
    """A stable, human-readable id for one extraction run."""
    import datetime as _dt
    import secrets as _sec
    ts = _dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return f"JOB_{ts}_{_sec.token_hex(3)}"


def _row_to_doc(row):
    """Reconstruct a result-shaped dict from a rover_documents row (dict-like).
    Prefer the full stored `raw` result (record/evidence, notes, …) when present;
    fall back to the flat columns for rows saved before `raw` existed."""
    raw = _loads(row.get("raw"))
    if isinstance(raw, dict) and raw:
        raw["_stored_id"] = row.get("doc_id")
        raw["job_id"] = row.get("job_id") or raw.get("job_id")
        raw["pdf"] = raw.get("pdf") or row.get("pdf")
        raw["approved"] = row.get("approved")
        raw["approved_at"] = row.get("approved_at")
        raw["approved_by"] = row.get("approved_by")
        return raw
    return {
        "pdf": row.get("pdf"),
        "values": _loads(row.get("header")) or {},
        "items": _loads(row.get("items")) or [],
        "needs_review": row.get("needs_review"),
        "suspect": _loads(row.get("suspect")),
        "reviewed": row.get("reviewed"),
        "cost": row.get("cost"),
        "job_id": row.get("job_id"),
        "approved": row.get("approved"),
        "approved_at": row.get("approved_at"),
        "approved_by": row.get("approved_by"),
        "_stored_id": row.get("doc_id"),
    }


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #
def save_document(result: dict) -> str:
    """UPSERT one extraction result; also replace its rover_items rows. Returns
    the doc_id (still returned even if the DB write fails, matching the file
    store's contract of always yielding an id)."""
    ensure_tables()
    doc_id = _doc_id(result)
    conn = None
    try:
        header = result.get("values") or {}
        items = result.get("items") or []
        # stable job id per extraction run (kept if the caller already set one)
        job_id = result.get("job_id") or _gen_job_id(doc_id)
        result["job_id"] = job_id
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO rover_documents
                (doc_id, pdf, header, items, needs_review, suspect, cost, raw, job_id,
                 approved, approved_at, approved_by)
            VALUES (?, ?, ?::jsonb, ?::jsonb, ?, ?::jsonb, ?, ?::jsonb, ?, ?, ?, ?)
            ON CONFLICT (doc_id) DO UPDATE SET
                pdf          = EXCLUDED.pdf,
                header       = EXCLUDED.header,
                items        = EXCLUDED.items,
                needs_review = EXCLUDED.needs_review,
                suspect      = EXCLUDED.suspect,
                cost         = EXCLUDED.cost,
                raw          = EXCLUDED.raw,
                job_id       = COALESCE(rover_documents.job_id, EXCLUDED.job_id),
                approved     = COALESCE(rover_documents.approved, EXCLUDED.approved),
                approved_at  = COALESCE(rover_documents.approved_at, EXCLUDED.approved_at),
                approved_by  = COALESCE(rover_documents.approved_by, EXCLUDED.approved_by)
            """,
            (
                doc_id,
                str(result.get("pdf") or "") or None,
                _dumps(header),
                _dumps(items),
                bool(result.get("needs_review")) if result.get("needs_review") is not None else None,
                _dumps(result.get("suspect")),
                _num(result.get("cost")),
                _dumps(result),          # full raw result — record/evidence, notes, etc.
                job_id,
                bool(result.get("approved")),
                result.get("approved_at"),
                result.get("approved_by"),
            ),
        )
        # Replace line-item rows for this doc.
        cur.execute("DELETE FROM rover_items WHERE doc_id = ?", (doc_id,))
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            cur.execute(
                """
                INSERT INTO rover_items
                    (doc_id, no, hs_code, description, quantity, unit, value)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_id,
                    _int(_item_get(item, "no", "No", "line_no", "index")) or (idx + 1),
                    _item_get(item, "hs_code", "HS Code", "hs"),
                    _item_get(item, "description", "item_name", "Item name", "name"),
                    _num(_item_get(item, "quantity", "Quantity", "qty")),
                    _item_get(item, "unit", "Unit", "uom"),
                    _num(_item_get(item, "value", "customs_value_mmk",
                                   "Customs Value (MMK)", "amount")),
                ),
            )
        conn.commit()
    except Exception as exc:
        print(f"[rover.store_pg.save_document] {exc}")
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return doc_id


def load_document(doc_id: str):
    """Load a single stored document by id, or None if missing/unreadable."""
    ensure_tables()
    conn = None
    try:
        conn = _conn()
        conn.row_factory = True  # dict rows
        cur = conn.cursor()
        cur.execute("SELECT * FROM rover_documents WHERE doc_id = ?", (str(doc_id),))
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_doc(row)
    except Exception as exc:
        print(f"[rover.store_pg.load_document] {exc}")
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def list_document_ids() -> list:
    """Return the ids of all stored documents (sorted, best-effort)."""
    ensure_tables()
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT doc_id FROM rover_documents ORDER BY doc_id")
        return [r[0] for r in cur.fetchall()]
    except Exception as exc:
        print(f"[rover.store_pg.list_document_ids] {exc}")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def all_documents() -> list:
    """Load every stored document, skipping any that fail to reconstruct."""
    ensure_tables()
    conn = None
    try:
        conn = _conn()
        conn.row_factory = True  # dict rows
        cur = conn.cursor()
        cur.execute("SELECT * FROM rover_documents ORDER BY doc_id")
        docs = []
        for row in cur.fetchall():
            try:
                docs.append(_row_to_doc(row))
            except Exception:
                continue
        return docs
    except Exception as exc:
        print(f"[rover.store_pg.all_documents] {exc}")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def approve_document(doc_id, corrections=None, user=None):
    """Approve one document: apply optional {column: value} corrections, mark it
    approved and move it out of the review queue. Persists via save_document (for
    the corrected values) then a direct UPDATE of the approval columns, because
    save_document's COALESCE would otherwise preserve the old approved=False.
    Fail-safe — returns the updated doc, or None on any error/missing doc."""
    ensure_tables()
    try:
        doc = load_document(doc_id)
        if doc is None:
            return None
        corrections = corrections or {}
        values = doc.setdefault("values", {})
        record = doc.get("record")
        confirmed = []
        for col, val in corrections.items():
            values[col] = val
            if isinstance(record, dict) and isinstance(record.get(col), dict):
                cell = record[col]
                cell["value"] = val
                cell["status"] = "confirmed"
                cell["model"] = "human"
            confirmed.append(col)
        doc["approved"] = True
        doc["approved_by"] = user
        doc["needs_review"] = False
        if confirmed:
            doc["suspect"] = [c for c in (doc.get("suspect") or []) if c not in confirmed]
        # persist the mutated values/record (approval cols are COALESCE-preserved
        # to the OLD False here, so stamp them directly right after).
        save_document(doc)
        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()
            cur.execute(
                "UPDATE rover_documents SET approved = TRUE, approved_at = now(), "
                "approved_by = ? WHERE doc_id = ?",
                (user, str(_doc_id(doc))),
            )
            conn.commit()
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return load_document(doc_id)
    except Exception as exc:
        print(f"[rover.store_pg.approve_document] {exc}")
        return None


# --------------------------------------------------------------------------- #
# row projections (grains) — same shapes as store.py, computed in Python
# --------------------------------------------------------------------------- #
def _doc_meta(doc):
    return {
        "doc_id": doc.get("_stored_id") or _doc_id(doc),
        "job_id": doc.get("job_id"),
        "document_name": doc.get("pdf"),
        "pdf": doc.get("pdf"),
        "approved": doc.get("approved"),
        "approved_at": doc.get("approved_at"),
        "approved_by": doc.get("approved_by"),
    }


def overall_rows() -> list:
    """One flat row per document: header values + doc_id/job_id/document_name/review."""
    rows = []
    for doc in all_documents():
        row = dict(doc.get("values") or {})
        row.update(_doc_meta(doc))
        row["needs_review"] = doc.get("needs_review")
        rows.append(row)
    return rows


def product_rows() -> list:
    """One flat row per line item: item fields + doc_id/job_id/document_name."""
    rows = []
    for doc in all_documents():
        meta = _doc_meta(doc)
        for item in doc.get("items") or []:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row.update(meta)
            rows.append(row)
    return rows


def joined_rows() -> list:
    """Each product row merged with its document's header values (item wins on
    clash). Documents with zero items contribute one header-only row."""
    rows = []
    for doc in all_documents():
        meta = _doc_meta(doc)
        header = dict(doc.get("values") or {})
        items = doc.get("items") or []
        if not items:
            row = dict(header)
            row.update(meta)
            rows.append(row)
            continue
        for item in items:
            row = dict(header)
            if isinstance(item, dict):
                row.update(item)  # item field wins on clash
            row.update(meta)      # doc_id/job_id/document_name always present
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
    """UPSERT a report definition; return its name."""
    ensure_tables()
    name = definition["name"]
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO rover_reports (name, grain, columns, filters, sort)
            VALUES (?, ?, ?::jsonb, ?::jsonb, ?)
            ON CONFLICT (name) DO UPDATE SET
                grain   = EXCLUDED.grain,
                columns = EXCLUDED.columns,
                filters = EXCLUDED.filters,
                sort    = EXCLUDED.sort
            """,
            (
                name,
                definition.get("grain", "overall"),
                _dumps(definition.get("columns") or []),
                _dumps(definition.get("filters") or []),
                definition.get("sort"),
            ),
        )
        conn.commit()
    except Exception as exc:
        print(f"[rover.store_pg.save_report] {exc}")
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return name


def load_report(name: str):
    """Load a saved report definition by name, or None if missing/unreadable."""
    ensure_tables()
    conn = None
    try:
        conn = _conn()
        conn.row_factory = True  # dict rows
        cur = conn.cursor()
        cur.execute("SELECT * FROM rover_reports WHERE name = ?", (str(name),))
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "name": row.get("name"),
            "grain": row.get("grain") or "overall",
            "columns": _loads(row.get("columns")) or [],
            "filters": _loads(row.get("filters")) or [],
            "sort": row.get("sort"),
        }
    except Exception as exc:
        print(f"[rover.store_pg.load_report] {exc}")
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def list_reports() -> list:
    """Return the names of all saved reports (sorted, best-effort)."""
    ensure_tables()
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT name FROM rover_reports ORDER BY name")
        return [r[0] for r in cur.fetchall()]
    except Exception as exc:
        print(f"[rover.store_pg.list_reports] {exc}")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def run_report(name: str) -> list:
    """Evaluate a saved report live (same grain/filter/column/sort logic as the
    file store — reuses store._passes / store._sort_key)."""
    ensure_tables()
    try:
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
    except Exception as exc:
        print(f"[rover.store_pg.run_report] {exc}")
        return []
