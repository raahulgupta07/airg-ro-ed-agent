"""V10 — Per-importer memory loader.

Loads:
  - importer_profiles.json (decl_no_prefix, currency_default, top_origins by HS)
  - consignors.json (canonical names)
  - field_misreads.json (common confusion patterns per importer)

Provides few-shot context for handwriting reads. The master/reader can include
"Past extractions for {importer}: decl_no usually starts {prefix}, currency
usually {ccy}, common HS codes {[...]}, common origins {[...]}".
"""
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional


KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"
PROFILES_JSON = KNOWLEDGE_DIR / "importer_profiles.json"
CONSIGNORS_JSON = KNOWLEDGE_DIR / "consignors.json"
MISREADS_JSON = KNOWLEDGE_DIR / "field_misreads.json"

KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)

# Best-effort SQLite paths (V7 DB)
DB_PATHS = [
    Path("/app/data/extraction_history.db"),
    Path("/app/data/jobs.db"),
    Path(__file__).parent.parent.parent / "data" / "extraction_history.db",
]


def _load_json(path: Path) -> Dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_json(path: Path, data: Dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        return True
    except Exception:
        return False


def _find_db() -> Optional[Path]:
    return next((p for p in DB_PATHS if p.exists()), None)


def get_importer_profile(importer_name: str) -> Dict:
    """Return cached profile + live SQLite stats for this importer.

    Output:
      {
        "importer": str,
        "n_past_extractions": int,
        "decl_no_prefix": str | None,        # e.g. "100296"
        "currency_default": str | None,      # e.g. "THB"
        "top_origins": [str],                # e.g. ["NZ", "TH"]
        "top_hs_codes": [str],
        "common_consignors": [str],
        "fee_baseline": dict | None,
        "known_misreads": [str],             # e.g. ["digit 7 of decl_no: 0<->8"]
      }
    """
    if not importer_name:
        return {}
    imp_upper = importer_name.upper()

    profiles = _load_json(PROFILES_JSON)
    cached = profiles.get(imp_upper, {})
    consignors = _load_json(CONSIGNORS_JSON)
    misreads = _load_json(MISREADS_JSON).get(imp_upper, [])

    out = dict(cached)
    out["importer"] = importer_name
    out["common_consignors"] = next((v for k, v in consignors.items() if k in imp_upper), [])
    out["known_misreads"] = misreads

    # Live SQLite enrichment (best effort)
    db = _find_db()
    if db:
        try:
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            # decl_no prefixes
            try:
                cur.execute("SELECT declaration_no FROM declarations "
                            "WHERE UPPER(importer_name) LIKE ? LIMIT 50",
                            (f"%{imp_upper}%",))
                decls = [r[0] for r in cur.fetchall() if r[0]]
                if decls:
                    out["n_past_extractions"] = len(decls)
                    # common 6-digit prefix
                    prefixes = Counter(d[:6] for d in decls if len(d) >= 6)
                    if prefixes:
                        top, n = prefixes.most_common(1)[0]
                        if n >= 2:
                            out["decl_no_prefix"] = top
            except Exception:
                pass
            # currency
            try:
                cur.execute("SELECT currency FROM declarations "
                            "WHERE UPPER(importer_name) LIKE ? "
                            "AND currency IS NOT NULL",
                            (f"%{imp_upper}%",))
                ccys = [r[0] for r in cur.fetchall()]
                if ccys:
                    out["currency_default"] = Counter(ccys).most_common(1)[0][0]
            except Exception:
                pass
            # origins per HS
            try:
                cur.execute("SELECT i.origin_country, i.hs_code FROM items i "
                            "JOIN declarations d ON d.job_id = i.job_id "
                            "WHERE UPPER(d.importer_name) LIKE ?",
                            (f"%{imp_upper}%",))
                rows = [(r[0], r[1]) for r in cur.fetchall() if r[0]]
                origins = Counter(r[0] for r in rows)
                hs_codes = Counter(r[1] for r in rows if r[1])
                out["top_origins"] = [o for o, _ in origins.most_common(5)]
                out["top_hs_codes"] = [h for h, _ in hs_codes.most_common(5)]
            except Exception:
                pass
            conn.close()
        except Exception:
            pass

    return out


def build_memory_prompt_block(profile: Dict) -> str:
    """Format profile as a few-shot context block to inject into LLM prompts."""
    if not profile or not profile.get("importer"):
        return ""
    parts = [f"## PRIOR KNOWLEDGE — Importer: {profile['importer']}"]
    if profile.get("n_past_extractions"):
        parts.append(f"- Past extractions: {profile['n_past_extractions']}")
    if profile.get("decl_no_prefix"):
        parts.append(f"- Declaration No usually starts with: {profile['decl_no_prefix']}")
    if profile.get("currency_default"):
        parts.append(f"- Currency typically: {profile['currency_default']}")
    if profile.get("top_origins"):
        parts.append(f"- Common origins: {', '.join(profile['top_origins'])}")
    if profile.get("top_hs_codes"):
        parts.append(f"- Common HS codes: {', '.join(profile['top_hs_codes'])}")
    if profile.get("common_consignors"):
        parts.append(f"- Known consignors (canonical): {', '.join(profile['common_consignors'])}")
    if profile.get("known_misreads"):
        parts.append(f"- Watch for past misreads: {'; '.join(profile['known_misreads'])}")
    parts.append("(Use this as a sanity check — but read what's actually on the form.)")
    return "\n".join(parts)


def record_correction(importer: str, field: str, original_value: str,
                       corrected_value: str) -> bool:
    """Append a user correction to field_misreads.json so future runs see the pattern."""
    if not importer:
        return False
    misreads = _load_json(MISREADS_JSON)
    key = importer.upper()
    misreads.setdefault(key, [])
    note = f"{field}: model said {original_value!r}, actual {corrected_value!r}"
    if note not in misreads[key]:
        misreads[key].append(note)
        # Keep only last 20 per importer
        misreads[key] = misreads[key][-20:]
    return _save_json(MISREADS_JSON, misreads)


def update_profile(importer: str, **kwargs) -> bool:
    """Update cached importer profile (e.g. fee_baseline)."""
    if not importer:
        return False
    profiles = _load_json(PROFILES_JSON)
    key = importer.upper()
    profiles.setdefault(key, {})
    profiles[key].update(kwargs)
    return _save_json(PROFILES_JSON, profiles)
