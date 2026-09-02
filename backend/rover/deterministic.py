"""Tier 0 — deterministic text-layer extraction. No LLM, $0. Owns the fields that
today's bake-off proved vision models get WRONG but the text layer nails:
  * declaration_no  — the TOP-OF-FORM 'Declaration No.' only (see note below)
  * declaration_date

Text layers on these CUSDECs are column-scrambled (label and value are a few lines
apart, order varies), so we locate a field by finding its LABEL line then taking the
nearest matching token within a small window either direction."""
import re
from typing import List, Optional, Tuple

from .schema import Cell

_DECLNO = re.compile(r"\b(\d{11,12})\b")
_DATE = re.compile(r"\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b")
_NUM = re.compile(r"\b(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+\.\d+)\b")
_CCY = re.compile(r"\b(THB|USD|SGD|CNY|EUR|JPY|AUD)\b")

# Currency -> plausible MMK-per-unit band. A printed rate outside its band is a
# mis-read (a stray total, qty, etc.) and is rejected.
_RATE_BANDS = {
    "THB": (40, 90),
    "USD": (1500, 5000),
    "SGD": (1500, 2500),
    "CNY": (250, 600),
    "EUR": (2000, 5000),
    "JPY": (12, 40),
    "AUD": (1200, 3500),
}


def _find_label(lines: List[str], *needles: str) -> Optional[int]:
    low = [l.lower() for l in lines]
    for i, l in enumerate(low):
        if all(n in l for n in needles):
            return i
    return None


def _nearest(lines: List[str], center: int, pat: re.Pattern, window: int = 5
             ) -> Optional[Tuple[int, "re.Match"]]:
    """Nearest regex match to `center` within +/- window. Returns (line_idx, match)."""
    best = None
    lo, hi = max(0, center - window), min(len(lines), center + window + 1)
    for i in range(lo, hi):
        m = pat.search(lines[i])
        if m:
            dist = abs(i - center)
            if best is None or dist < best[0]:
                best = (dist, i, m)
    return (best[1], best[2]) if best else None


def _iso(y: str, m: str, d: str) -> str:
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


_MA_STEM = re.compile(r"^(MA\d{4})(\d+)", re.IGNORECASE)


def ma_decl_no_from_name(filename: str) -> Optional[str]:
    """Old-format (pre-MACCS) 'MA' declarations carry their number as a hard-to-OCR
    Registration-No stamp; the file name is authoritative and matches it. Team files
    these as 'MA0259/100405' (station 'MA0259' + tail). Returns None for non-MA names."""
    stem = (filename or "").strip()
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    m = _MA_STEM.match(stem)
    if not m:
        return None
    return f"{m.group(1).upper()}/{m.group(2)}"


def declaration_no(lines: List[str]) -> Cell:
    c = Cell(column="declaration_no", model="deterministic")
    # Business rule (changed 2026-08-01, team decision): the top-of-form
    # 'Declaration No.' identifies THIS document. 'First approval declaration
    # No.' is a DIFFERENT earlier declaration that an Ex-bond release refers
    # back to — using it labelled the document with the wrong number.
    # The top-of-form 'Declaration No.' — and ONLY that one.
    dn = _find_label(lines, "declaration no")
    if dn is not None:
        hit = _nearest(lines, dn, _DECLNO, window=5)
        if hit:
            c.value, c.confidence, c.status = hit[1].group(1), 0.95, "ok"
            c.source = f"line~{hit[0]} near 'Declaration No.'"
            # Say so when a first-approval number is also present and differs, so a
            # reviewer can see which of the two numbers on the page was chosen.
            fa = _find_label(lines, "first approval", "declaration")
            if fa is not None:
                fa_hit = _nearest(lines, fa, _DECLNO, window=5)
                if fa_hit and fa_hit[1].group(1) != c.value:
                    c.note = (f"form also carries first-approval no. {fa_hit[1].group(1)} "
                              f"(a different, earlier declaration) — not used")
            return c
    c.status = "empty"
    return c


def declaration_no_official(lines: List[str], first_approval=None) -> Cell:
    """The doc's own 'Declaration No.' field (distinct from the First-approval no.).
    Captured as its OWN column so the user can export either — the two legitimately
    differ on some forms and the ledger may key on either."""
    c = Cell(column="declaration_no_official", model="deterministic")
    dn = _find_label(lines, "declaration no")
    if dn is not None:
        # prefer a number that is NOT the first-approval value (they sit apart)
        lo, hi = max(0, dn - 5), min(len(lines), dn + 6)
        best = None
        for i in sorted(range(lo, hi), key=lambda i: abs(i - dn)):
            m = _DECLNO.search(lines[i])
            if m and (first_approval is None or m.group(1) != str(first_approval)):
                best = (i, m.group(1)); break
        if best:
            c.value, c.confidence, c.status = best[1], 0.9, "ok"
            c.source = f"line~{best[0]} near 'Declaration No.'"
            return c
    c.status = "empty"
    return c


def declaration_date(lines: List[str]) -> Cell:
    c = Cell(column="declaration_date", model="deterministic")
    lab = _find_label(lines, "declaration date")
    if lab is not None:
        hit = _nearest(lines, lab, _DATE, window=4)
        if hit:
            c.value = _iso(*hit[1].groups())
            c.confidence, c.status = 0.95, "ok"
            c.source = f"line~{hit[0]} near 'Declaration date'"
            return c
    c.status = "empty"
    return c


def _labeled_date(lines: List[str], column: str, label: str,
                  exclude: str = "", window: int = 4) -> Cell:
    """Generic date-near-label reader. Tries EVERY occurrence of the label (some
    labels appear on more than one page — e.g. a blank 'Arrival Date :' on the
    port delivery order before the filled one on the CUSDEC) and keeps the first
    that actually yields a date. `exclude` skips false labels ('Release order
    notification' page title vs the dated 'Release order' row)."""
    c = Cell(column=column, model="deterministic")
    low = [l.lower() for l in lines]
    for i, l in enumerate(low):
        if label not in l:
            continue
        if exclude and exclude in l:
            continue
        hit = _nearest(lines, i, _DATE, window=window)
        if hit:
            c.value = _iso(*hit[1].groups())
            # 0.95 is the threshold at which supervisor.merge lets a
            # deterministic read beat the model. At 0.93 these three dates were
            # model-owned whenever the model answered at all, so the printed
            # characters lost to a guess and the values moved with the model
            # version. This reader anchors on the label and excludes the known
            # false ones; where it finds nothing it stays empty and vision still
            # fills the gap, so scanned pages are unaffected.
            c.confidence, c.status = 0.95, "ok"
            c.source = f"line~{hit[0]} near '{label}'"
            return c
    c.status = "empty"
    return c


def arrival_date(lines: List[str]) -> Cell:
    return _labeled_date(lines, "arrival_date", "arrival date")


def release_order_date(lines: List[str]) -> Cell:
    return _labeled_date(lines, "release_order_date", "release order",
                         exclude="notification", window=3)


def completion_date(lines: List[str]) -> Cell:
    return _labeled_date(lines, "completion_date", "declaration completion",
                         window=3)


def exchange_rate(lines: List[str]) -> Cell:
    c = Cell(column="exchange_rate", model="deterministic")
    lab = _find_label(lines, "exchange rate")
    if lab is not None:
        # Currency may sit on the label line or drift a few lines away.
        ccy = None
        lo, hi = max(0, lab - 3), min(len(lines), lab + 4)
        for i in range(lo, hi):
            mc = _CCY.search(lines[i].upper())
            if mc:
                ccy = mc.group(1)
                break
        band = _RATE_BANDS.get(ccy) if ccy else (12, 5000)
        # Walk the window in distance order from the label; take the first
        # numeric token whose value lands inside the band.
        window = 6
        wlo, whi = max(0, lab - window), min(len(lines), lab + window + 1)
        order = sorted(range(wlo, whi), key=lambda i: abs(i - lab))
        for i in order:
            for m in _NUM.finditer(lines[i]):
                try:
                    val = float(m.group(1).replace(",", ""))
                except ValueError:
                    continue
                if band[0] <= val <= band[1]:
                    c.value, c.confidence, c.status = val, 0.9, "ok"
                    c.source = f"line~{i} near 'Exchange Rate'"
                    if ccy:
                        c.note = f"{ccy} rate (band {band[0]}-{band[1]})"
                    return c
    c.status = "empty"
    return c


def extract(lines: List[str]) -> dict:
    """Return {column: Cell} for the deterministic-owned fields (empty Cells if the
    text layer is absent/scanned — vision tier then fills them)."""
    dn = declaration_no(lines)
    return {
        "declaration_no": dn,
        "declaration_no_official": declaration_no_official(lines, first_approval=dn.value),
        "declaration_date": declaration_date(lines),
        "arrival_date": arrival_date(lines),
        "release_order_date": release_order_date(lines),
        "completion_date": completion_date(lines),
        "exchange_rate": exchange_rate(lines),
    }
