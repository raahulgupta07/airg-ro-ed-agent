"""One parser for the amounts printed on a customs form.

There were ten near-copies of `_num()` across the backend (workflow, supervisor,
mapping, cube, store_pg, excel, issues, cusdec_rescue, vision_rescue, and a
script). Nine of them were `float(str(v).replace(",", ""))`, which loses the
value the moment the form prints a currency with it:

    "THB 652,279.7184"   -> None          (or, in the ROVER bridge, the string
    "1,394,615 MMK"      -> None           itself, which Postgres then rejects,
                                           aborting the whole item batch)

The failure is silent and site-specific: an export writes a blank cell, the math
supervisor can't run its identity, `issues.py` reports a present value missing.
Fixing one copy fixes one symptom, so they all now call this.

**What this deliberately does NOT do** is strip every non-digit character. That
turns "2026/01/08" into 20260108 and the MA-series id "MA0259/100405" into a
number. The parser accepts a currency token at either end and thousands
separators in the middle, then requires what remains to be a clean number —
anything else raises. `_num`-style callers that want the old quiet behaviour use
`to_float`; the DB writers want the loud one so a value we cannot read never
becomes 0 in a money column.

No imports beyond `re` — `rover/` is self-contained by design and must not pull
in the database layer to parse a number.
"""
import re

# What a customs form prints when an amount does not apply. "—" and "–" are the
# unicode dashes that come out of the PDF text layer, not typos for "-".
BLANK_MARKERS = {"", "-", "--", "—", "–", "/", "n/a", "na", "none", "null"}

# A currency token sitting before or after the amount. The text layer prints
# "THB 1626905.9000" and "1626905.9000 THB"; the form's value block renders as
# "Invoice price A - CIF - THB- 481,406.664", so a row-joined read carries the
# label joiner along with it.
#
# This is an explicit list, not `[A-Za-z]{1,4}`. Replaying every value ROVER has
# actually produced through the loose pattern turned the invoice reference
# "A- 9518633846" into the float 9518633846.0 and reduced the source string
# "Rate 64.408" to a bare number — it treats any short word as a currency, which
# is precisely the label-stripping this parser is supposed to refuse.
_CURRENCIES = (
    "THB", "MMK", "USD", "EUR", "SGD", "JPY", "CNY", "GBP", "INR",
    "AUD", "HKD", "MYR", "KRW", "VND", "KS", "K",
)
_CUR_ALT = "|".join(sorted(_CURRENCIES, key=len, reverse=True))
_LEADING_CURRENCY = re.compile(rf"^(?:{_CUR_ALT}|[$€£¥₹])\s*", re.IGNORECASE)
_TRAILING_CURRENCY = re.compile(rf"\s*(?:{_CUR_ALT})$", re.IGNORECASE)

# A dash left behind after the currency token is dropped. "THB- 481,406.664" is
# a positive invoice price with a joiner, not a negative amount — and it is on
# every form, so reading it as negative would corrupt the common case. A real
# minus binds tight to its digits ("-481406.664"), a joiner has a space after
# it. That is the only signal available; where it is absent we keep the sign.
_JOINER_DASH = re.compile(r"^-\s+(?=[\d(])")

# What must remain once separators and currency are gone. Anchored on purpose:
# a partial match is how "2026/01/08" would sneak through as a number.
_CLEAN_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def parse_amount(value):
    """Parse a form-printed amount to float. None for a blank marker.

    Raises ValueError when the text holds no unambiguous number — including
    dates and slash-form declaration numbers, which must never be coerced.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is an int subclass; True would silently become 1.0.
        raise ValueError(f"not a number: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip()
    if s.lower() in BLANK_MARKERS:
        return None

    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1].strip()

    had_currency = bool(_LEADING_CURRENCY.match(s))
    s = _LEADING_CURRENCY.sub("", s)
    s = _TRAILING_CURRENCY.sub("", s)
    if had_currency:
        s = _JOINER_DASH.sub("", s, count=1)
    s = s.replace(",", "").replace(" ", "").strip()

    if not _CLEAN_NUMBER.match(s):
        raise ValueError(f"not a number: {value!r}")

    n = float(s)
    return -n if negative else n


def to_float(value, default=None):
    """`parse_amount` for callers that want None (or `default`) instead of a raise.

    This is the drop-in for the old `_num()` contract.
    """
    try:
        n = parse_amount(value)
    except (ValueError, TypeError):
        return default
    return default if n is None else n


def keep_if_unparseable(value):
    """Coerce when it is an amount, otherwise hand back the original untouched.

    The ROVER→DB bridge needs this: it runs whole records through one mapper
    where dates, importer names and amounts are mixed together, and a date must
    survive the trip unchanged.
    """
    if value is None or isinstance(value, (int, float)):
        return value
    try:
        return parse_amount(value)
    except (ValueError, TypeError):
        return value
