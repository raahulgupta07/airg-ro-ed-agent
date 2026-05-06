"""V9 — Knowledge base lookup tools (canonical names, HS codes, origin history)."""
import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, Optional
from agno.tools import tool

KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"
CONSIGNORS_JSON = KNOWLEDGE_DIR / "consignors.json"


def _load_consignors() -> Dict:
    try:
        return json.loads(CONSIGNORS_JSON.read_text())
    except Exception:
        return {}


@tool(show_result=False)
def consignor_canonical(name: str, importer: str) -> Dict:
    """Map a consignor name to its canonical form for a given importer.

    Uses seeded knowledge base. Matches if first word matches (case-insensitive).
    Useful when OCR returns slight variants like 'ASIATIC MAST' for 'Asiatic Mart'.

    Returns {"canonical": str, "matched": bool, "candidates": [str]}."""
    if not name or not importer:
        return {"canonical": name, "matched": False, "candidates": []}
    seeds = _load_consignors()
    imp_key = next((k for k in seeds if k in importer.upper()), None)
    if not imp_key:
        return {"canonical": name, "matched": False, "candidates": []}
    candidates = seeds[imp_key]
    name_first = name.lower().split()[0] if name.split() else ""
    for canon in candidates:
        canon_first = canon.lower().split()[0] if canon.split() else ""
        if canon_first and name_first and canon_first == name_first:
            return {"canonical": canon, "matched": True, "candidates": candidates}
    return {"canonical": name, "matched": False, "candidates": candidates}


@tool(show_result=False)
def hs_lookup(code: str) -> Dict:
    """Normalize and validate an HS code.

    Accepts forms: 0406.10.10, 0406.10.10.00, 0406.10.10 00, 0406101000.
    Returns canonical 'NNNN.NN.NN NN' form (with space before final 2 digits).

    Returns {"normalized": str, "valid": bool, "digits": str}."""
    if not code:
        return {"normalized": "", "valid": False, "digits": ""}
    digits = re.sub(r"[^\d]", "", code)
    if len(digits) < 8:
        return {"normalized": code, "valid": False, "digits": digits}
    # pad to 10 digits
    if len(digits) == 8:
        digits = digits + "00"
    digits = digits[:10]
    normalized = f"{digits[:4]}.{digits[4:6]}.{digits[6:8]} {digits[8:10]}"
    return {"normalized": normalized, "valid": True, "digits": digits}


@tool(show_result=False)
def origin_history(importer: str, hs_code: str) -> Dict:
    """Look up most-common origin country from past extractions of same importer + HS prefix.

    Best-effort: reads V7's SQLite DB if available. Returns most-frequent origin code
    seen for (importer, HS-6-digit-prefix) combo, or empty if no history.

    Returns {"top_origin": Optional[str], "count": int, "samples": [str]}."""
    digits = re.sub(r"[^\d]", "", hs_code or "")
    hs6 = digits[:6] if len(digits) >= 6 else ""
    if not importer or not hs6:
        return {"top_origin": None, "count": 0, "samples": []}
    db_paths = [
        Path("/app/data/jobs.db"),
        Path("/app/data/db.sqlite"),
        Path("/app/database.db"),
        Path(__file__).parent.parent.parent.parent / "backend" / "data" / "jobs.db",
    ]
    db_path = next((p for p in db_paths if p.exists()), None)
    if not db_path:
        return {"top_origin": None, "count": 0, "samples": []}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # Best-effort: try common table/column names; swallow errors
        rows = []
        for sql in [
            ("SELECT i.origin_country FROM items i "
             "JOIN declarations d ON d.job_id = i.job_id "
             "WHERE UPPER(d.importer_name) LIKE ? AND i.hs_code LIKE ? "
             "AND i.origin_country IS NOT NULL"),
        ]:
            try:
                cur.execute(sql, (f"%{importer.upper()}%", f"{hs6[:4]}.{hs6[4:6]}%"))
                rows = [r[0] for r in cur.fetchall() if r[0]]
                if rows:
                    break
            except Exception:
                continue
        conn.close()
        if not rows:
            return {"top_origin": None, "count": 0, "samples": []}
        from collections import Counter
        c = Counter(rows)
        top, n = c.most_common(1)[0]
        return {"top_origin": top, "count": n, "samples": list(c)[:5]}
    except Exception:
        return {"top_origin": None, "count": 0, "samples": []}
