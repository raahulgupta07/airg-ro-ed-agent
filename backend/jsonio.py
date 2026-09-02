"""Read a JSON column whose storage type may be `text` or `jsonb`.

Migration 0007 converted eleven `*_json` columns from `text` to `jsonb`. psycopg3
adapts `jsonb` on the way out, so a column that used to arrive as a `str` now
arrives as a `dict`/`list` already parsed. Every existing caller did:

    json.loads(row["evidence_json"] or "{}")

which raises `TypeError: the JSON object must be str, bytes or bytearray, not
dict` the moment the column becomes jsonb. Two of those callers sit inside a bare
`except Exception: continue`, so the failure is SILENT: `routes/evidence.py`
skipped every row and the checks page rendered empty with no error anywhere.

`loads_maybe` accepts either representation, so the same code works before and
after the migration, and on a database that is mid-upgrade.
"""
from __future__ import annotations

import json
from typing import Any


def loads_maybe(value: Any, default: Any = None) -> Any:
    """Return `value` as a Python object, whatever the column type gave us.

    * already a dict/list (jsonb)  -> returned unchanged
    * str/bytes (text)             -> parsed
    * None / '' / unparseable      -> `default` (defaults to `{}`)

    Never raises. A malformed payload is data, not a reason to fail the request —
    but note it returns `default`, so a caller that needs to tell "empty" from
    "corrupt" must check the raw value itself.
    """
    if default is None:
        default = {}
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return default
