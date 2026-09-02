"""A response model that disagrees with the column it serves returns 500, silently.

`items.quantity` became `numeric` in migration 0007. `ItemResponse.quantity`
stayed `Optional[str]`. psycopg hands back a `Decimal`, Pydantic v2 refuses to
coerce it to `str`, and validation raised on the first row — so
`GET /api/data/items` answered 500 for every request while 799 items sat in the
table. Nothing appeared in the log, and the page's own empty state explained the
blank screen convincingly: "No product items yet."

It is the same failure family as the `database.py` type edits that were inert and
the `_save_to_db` whitelist that dropped fields: a second description of the
schema, written by hand, drifting away from the one that runs.

Derived from the migration chain via `_ddl_sources`, so it needs no database.
"""
from __future__ import annotations

import typing

import pytest

from tests._ddl_sources import alembic_types

# Pydantic model -> the table it serves. Only models that map a table row 1:1.
MODELS = {}
try:
    import schemas
    MODELS = {
        "ItemResponse": (schemas.ItemResponse, "items"),
        "DeclarationResponse": (schemas.DeclarationResponse, "declarations"),
    }
except Exception as exc:                     # pragma: no cover
    pytest.skip(f"schemas not importable: {exc}", allow_module_level=True)

# Which Python annotations can carry which Postgres types. A numeric column can
# be served as float or Decimal; it cannot be served as `str`.
_ALLOWED = {
    "numeric": {float, int, "Decimal"},
    "double precision": {float, int},
    "real": {float, int},
    "integer": {int, float},
    "smallint": {int, float},
    "bigint": {int, float},
    "boolean": {bool, int},
    "text": {str},
    "character varying": {str},
    "jsonb": {dict, list, str},
    "json": {dict, list, str},
}


def _inner_types(annotation) -> set:
    """The concrete types inside `Optional[X]` / `Union[...]`."""
    args = typing.get_args(annotation)
    if not args:
        return {annotation}
    return {a for a in args if a is not type(None)}


def _names(types) -> set:
    return {getattr(t, "__name__", str(t)) for t in types}


@pytest.mark.parametrize("model_name", sorted(MODELS))
def test_every_field_can_hold_what_its_column_returns(model_name):
    model, table = MODELS[model_name]
    columns, _unparsed = alembic_types()

    wrong = []
    for field, info in model.model_fields.items():
        col_type = columns.get((table, field))
        if col_type is None:
            continue                          # not a column of this table
        allowed = _ALLOWED.get(col_type)
        if allowed is None:
            continue                          # date/time and friends — not the risk here
        declared = _inner_types(info.annotation)
        ok = any(d in allowed or getattr(d, "__name__", "") in allowed for d in declared)
        if not ok:
            wrong.append(f"{model_name}.{field}: declared {sorted(_names(declared))}, "
                         f"column is {col_type}")
    assert wrong == [], (
        "\n".join(wrong)
        + "\n\nA mismatch here is a 500 on every request to the endpoint that "
          "serves this model, with nothing in the log.")
