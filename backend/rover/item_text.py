"""Tier 0 — deterministic text-layer parser for item QUANTITIES. No LLM, $0.

On digital customs pages the item table lives in the PDF text layer but is
column-scrambled: HS codes, descriptions, units and quantities land on separate
lines in a shifting order. The vision lane frequently returns items with
`quantity=null`/`unit=null` even though the number is right there in the text.

This module recovers `(hs_code, description, quantity, unit)` rows from the text
layer so the pipeline can cross-check and gap-fill vision output cheaply. It is
best-effort and fail-safe by contract: every public function returns a sensible
empty/unchanged value on any error and never raises into the pipeline.
"""
# `X | None` annotations are evaluated at def time before 3.10. The image runs
# 3.12, but a dev box on the system python (3.9 on macOS) cannot even import this
# module — which takes rover.pipeline_fast down with it and blocks the whole test
# suite at collection. Deferring annotations costs nothing and keeps both alive.
from __future__ import annotations

import re

# HS code like 1806.20.90 or 1806.20.90 00 (trailing 2-digit statistical suffix).
_HS = re.compile(r"\b(\d{4}\.\d{2}\.\d{2}(?:\s?\d{2})?)\b")
# A number with optional thousands separators and decimals.
_QTY = re.compile(r"\b(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\b")
# Unit tokens seen on Myanmar CUSDEC item rows (matched case-insensitively).
_UNITS = ("KG", "KGM", "CTN", "CARTON", "PCS", "PC", "UNIT", "L", "LTR", "MT", "G")

# Numbers this large are customs values / totals, never a line quantity.
_MAX_QTY = 10_000_000
# How many lines after the HS line form the extraction window.
_WINDOW = 6


def _norm_hs(hs: str) -> str:
    """Collapse internal whitespace in an HS match to a single space."""
    return re.sub(r"\s+", " ", hs).strip()


def _digits(s: str) -> str:
    """Digits-only view of a string, for HS-code identity compares."""
    return re.sub(r"\D", "", s or "")


def _squash(s: str) -> str:
    """Uppercase, no-spaces view of a description, for fuzzy contains compares."""
    return re.sub(r"\s+", "", (s or "").upper())


def _is_description(line: str) -> bool:
    """Heuristic: a real description line has >12 letters and doesn't start with a digit."""
    if not line or line[0].isdigit():
        return False
    return sum(c.isalpha() for c in line) > 12


def _find_unit(line: str) -> str | None:
    """Return the first `_UNITS` token appearing as a whole word in the line, else None."""
    for tok in re.findall(r"[A-Za-z]+", line):
        if tok.upper() in _UNITS:
            return tok.upper()
    return None


def _plausible_qty(line: str, hs_digits: str) -> float | None:
    """First numeric token on `line` that looks like a quantity (not the HS digits, not a value)."""
    for m in _QTY.finditer(line):
        raw = m.group(1)
        # Skip the HS code's own digits masquerading as a number.
        if _digits(raw) and _digits(raw) in hs_digits:
            continue
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        if val <= 0 or val > _MAX_QTY:
            continue
        return val
    return None


def parse_items(ctx) -> list[dict]:
    """Best-effort extract of item rows from the text layer.

    For each HS-code occurrence on an item-like page, gather the HS line plus the
    next ~6 lines and pull hs_code, description, unit and a plausible quantity.
    Returns a list of {"hs_code","description","quantity","unit"} dicts (one per
    HS occurrence, exact (hs_code, description) repeats de-duped). Returns [] on
    any error.
    """
    try:
        rows: list[dict] = []
        seen: set[tuple] = set()
        for page in ctx.pages:
            text = getattr(page, "text", "") or ""
            # Only pages that look like item tables.
            if not (_HS.search(text) or "quantity" in text.lower()):
                continue
            lines = getattr(page, "lines", []) or []
            for i, line in enumerate(lines):
                m = _HS.search(line)
                if not m:
                    continue
                hs_code = _norm_hs(m.group(1))
                hs_digits = _digits(hs_code)
                window = lines[i:i + _WINDOW + 1]

                # Description: longest description-like line in the window.
                description = ""
                for w in window:
                    if _is_description(w) and len(w) > len(description):
                        description = w

                # Unit: first `_UNITS` token, remembering its window index.
                unit = None
                unit_idx = None
                for wi, w in enumerate(window):
                    u = _find_unit(w)
                    if u:
                        unit, unit_idx = u, wi
                        break

                # Quantity: prefer the unit's line / its neighbours, then any line.
                quantity = None
                if unit_idx is not None:
                    for wi in (unit_idx, unit_idx - 1, unit_idx + 1):
                        if 0 <= wi < len(window):
                            quantity = _plausible_qty(window[wi], hs_digits)
                            if quantity is not None:
                                break
                if quantity is None:
                    for w in window:
                        quantity = _plausible_qty(w, hs_digits)
                        if quantity is not None:
                            break

                key = (hs_code, description)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "hs_code": hs_code,
                    "description": description,
                    "quantity": quantity,
                    "unit": unit,
                })
        return rows
    except Exception:
        return []


def fill_quantities(vision_items: list[dict], ctx) -> list[dict]:
    """Fill only MISSING quantity/unit on vision items from the text layer.

    Matches text-layer rows to vision items by HS code (digits-only) and, when
    several vision items share an HS code, by nearest description (uppercase
    no-space containment). Never overwrites a value the vision lane already has —
    only `None`/missing fields are filled. Returns copies; inputs are not mutated.
    Returns `vision_items` unchanged on any error.
    """
    try:
        parsed = parse_items(ctx)
        if not parsed:
            return vision_items

        # Index parsed rows by HS digits.
        by_hs: dict[str, list[dict]] = {}
        for row in parsed:
            by_hs.setdefault(_digits(row.get("hs_code", "")), []).append(row)

        out: list[dict] = []
        for item in vision_items:
            copy = dict(item)
            hs_digits = _digits(copy.get("hs_code", ""))
            candidates = by_hs.get(hs_digits, []) if hs_digits else []

            match = None
            if len(candidates) == 1:
                match = candidates[0]
            elif len(candidates) > 1:
                want = _squash(copy.get("description") or copy.get("item_name") or "")
                for cand in candidates:
                    have = _squash(cand.get("description", ""))
                    if want and have and (want in have or have in want):
                        match = cand
                        break
                if match is None:
                    match = candidates[0]

            if match:
                if copy.get("quantity") is None and match.get("quantity") is not None:
                    copy["quantity"] = match["quantity"]
                if copy.get("unit") is None and match.get("unit") is not None:
                    copy["unit"] = match["unit"]
            out.append(copy)
        return out
    except Exception:
        return vision_items
