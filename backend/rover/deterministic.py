"""Tier 0 — deterministic text-layer extraction. No LLM, $0. Owns the fields that
today's bake-off proved vision models get WRONG but the text layer nails:
  * declaration_no  — with the business rule: prefer 'First approval declaration No.'
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


def declaration_no(lines: List[str]) -> Cell:
    c = Cell(column="declaration_no", model="deterministic")
    # Business rule: First approval declaration No. wins when present.
    fa = _find_label(lines, "first approval", "declaration")
    if fa is not None:
        hit = _nearest(lines, fa, _DECLNO, window=5)
        if hit:
            c.value, c.confidence, c.status = hit[1].group(1), 0.98, "ok"
            c.source = f"line~{hit[0]} near 'First approval declaration No.'"
            c.note = "first-approval rule"
            return c
    # Fallback: the plain 'Declaration No.' field.
    dn = _find_label(lines, "declaration no")
    if dn is not None:
        hit = _nearest(lines, dn, _DECLNO, window=5)
        if hit:
            c.value, c.confidence, c.status = hit[1].group(1), 0.90, "ok"
            c.source = f"line~{hit[0]} near 'Declaration No.'"
            c.note = "declaration-no fallback (no first-approval found)"
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
        "exchange_rate": exchange_rate(lines),
    }
