"""The boot banner printed the database password.

`✅ Database initialized (postgres) — DSN: postgresql+psycopg://ro_ed:<password>
@postgres:5432/ro_ed`, once per uvicorn worker, on every start — so the
credential sat in `docker logs` and in anything shipping those logs onward.

The line is worth keeping: host, port and database name are what it exists to
report, and "which database did this container actually attach to" has been a
real question here. Only the password goes.
"""
from __future__ import annotations

import pathlib

import pytest

DB_PY = pathlib.Path(__file__).resolve().parents[1] / "database.py"


@pytest.mark.parametrize("dsn,expected", [
    ("postgresql+psycopg://ro_ed:ro_ed_dev_pass@postgres:5432/ro_ed",
     "postgresql+psycopg://ro_ed:***@postgres:5432/ro_ed"),
    ("postgresql://user:p%40ss:word@host:5432/db",
     "postgresql://user:***@host:5432/db"),
    # No credentials at all — nothing to hide, nothing to mangle.
    ("postgresql://postgres:5432/ro_ed", "postgresql://postgres:5432/ro_ed"),
    ("", ""),
    (None, ""),
])
def test_the_password_is_replaced_and_the_rest_survives(dsn, expected):
    import database
    assert database._masked_dsn(dsn) == expected


def test_a_password_containing_an_at_sign_still_goes():
    """`@` inside a password is legal and would defeat a left-to-right split."""
    import database
    got = database._masked_dsn("postgresql://ro_ed:pa@ss@postgres:5432/ro_ed")
    assert "pa@ss" not in got
    assert got.endswith("@postgres:5432/ro_ed")


def test_the_banner_does_not_print_the_raw_dsn():
    """The negative control: masking is useless if the caller still interpolates
    `db_engine.DATABASE_URL` directly."""
    src = DB_PY.read_text(encoding="utf-8")
    assert "DSN: {db_engine.DATABASE_URL}" not in src, (
        "the boot banner still prints the DSN verbatim, password included")
    assert "_masked_dsn(db_engine.DATABASE_URL)" in src
