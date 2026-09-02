"""One reader for the dates printed on a customs bundle.

The four date columns on `declarations` are TEXT, and nothing normalises what
goes into them. A survey of the 23 stored declarations found three spellings
living in the same column:

    2025-06-25    ISO, 20 rows
    2024/04/01    MACCS year-first, 2 rows
    12/10/2025    licence day-first, 1 row

Mixed formats make the column unsortable, unfilterable, and impossible to
compare against the team's ledger without a bespoke parser at every call site —
which is exactly why a probe of mine reported three dates "missing" that were
sitting in the database the whole time, just spelled with slashes.

**Which slash form is which** is settled by the documents, not by guesswork. A
bundle carries both:

  * The MACCS release-order pages print `2025/10/27`, `2025/10/29` — year first,
    unambiguous because the 4-digit year leads.
  * The Ministry of Commerce licence pages print `27/09/2029`, `19/10/2025`,
    `22/06/2025` — day first, proven by the days above 12, which cannot be
    months.

So a 4-digit leading group is year-first and a 4-digit trailing group is
day-first. Nothing here has to guess between D/M and M/D.

Times are discarded: the form prints `Declaration date 2024/04/01 13:12` and the
column holds a date.
"""
import re

# What a form prints when a date does not apply. "/  /" is the empty date box
# on the release order, which appears verbatim in the text layer.
BLANK_MARKERS = {"", "-", "--", "—", "–", "/", "//", "/ /", "/  /",
                 "n/a", "na", "none", "null"}

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_YEAR_FIRST = re.compile(r"^(\d{4})[/.](\d{1,2})[/.](\d{1,2})")
_DAY_FIRST = re.compile(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})")


def _build(y, m, d):
    y, m, d = int(y), int(m), int(d)
    if not (1 <= m <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100):
        raise ValueError("not a real date: %04d-%02d-%02d" % (y, m, d))
    return "%04d-%02d-%02d" % (y, m, d)


def to_iso(value):
    """Normalise a printed date to ISO. None for a blank marker.

    Raises ValueError on anything that is not one of the three forms above —
    a date column should not quietly accept a declaration number.
    """
    if value is None:
        return None
    s = str(value).strip()
    if s.lower() in BLANK_MARKERS:
        return None

    # Drop a trailing time ("2024/04/01 13:12") and any surrounding brackets.
    s = s.strip("()[] ")
    s = re.split(r"[ T]", s, 1)[0]

    for pat, order in ((_ISO, "ymd"), (_YEAR_FIRST, "ymd"), (_DAY_FIRST, "dmy")):
        m = pat.match(s)
        if not m:
            continue
        a, b, c = m.groups()
        return _build(a, b, c) if order == "ymd" else _build(c, b, a)

    raise ValueError("not a date: %r" % (value,))


def normalise(value):
    """ISO when it parses, the original value untouched when it does not.

    The engine→DB bridge maps whole records in one pass, so a value that is not
    a date has to survive the trip rather than becoming None.
    """
    if value is None:
        return None
    try:
        iso = to_iso(value)
    except ValueError:
        return value
    return iso if iso is not None else None
