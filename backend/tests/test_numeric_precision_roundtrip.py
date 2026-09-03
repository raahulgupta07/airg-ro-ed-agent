"""A money column must give back the number that was put into it.

`items.customs_value_mmk` was Postgres `real` — IEEE-754 single precision, about
7 significant decimal digits. A Myanmar customs value needs 10 or 11. So the form
value 19,363,898.77 was stored and read back as 19,363,898.0; 9,580,894.17 as
9,580,894.0; 486,007.83 as 486,007.84. Those exact wrong figures reached the
customer's Excel file.

Every test in this repo passed throughout, because nothing was broken in a way a
test was looking at: a float column got a valid float, no exception, no gate
tripped. The arithmetic gates could not see it either — the CIF identity is
checked to a percentage tolerance and a rounding error of one part in ten million
is inside every tolerance we have. The only thing that would have caught it is
someone asserting that the number came back unchanged.

Three layers here, weakest claim to strongest:

  1. `TestWhatSinglePrecisionDoesToTheseNumbers` — pure arithmetic, no database.
     Pins that these specific figures really are unrepresentable in float32 and
     fine in float64. This is the specification, not the guard; it passes on a
     broken schema and on a fixed one alike.

  2. `TestTheMigrationsWidenEveryMoneyColumn` — reads the type each money column
     ends up with after replaying the migration chain, and asserts it can hold
     the figures from layer 1. This is the guard that runs everywhere, including
     a laptop with no database. It fails on a schema that still says `real`.

  3. `TestTheLiveColumnRoundTripsTheRealValues` — writes each value into a column
     of the type the LIVE database actually uses and reads it back. The only
     layer that can catch "the migration exists but was never applied", which is
     the state this database was in when the defect was found. Skips when no
     database is reachable, so it proves nothing on a machine without one.

WHAT NONE OF THESE PROVE: that the number written was the right number. A wrong
figure stored exactly is still a wrong figure — that is what the CIF and item-sum
gates and `test_unit_consistency.py` are for. This file only proves the database
is not the thing corrupting it.
"""
import os
import struct

import pytest

import db_engine
from tests._ddl_sources import effective_types


# ── the real failures, taken from the review form and the stored column ──────
# Written as (value, what the `real` column gave back). The second element is not
# a guess: it is what float32 rounds to, reproduced exactly below.
OBSERVED_TRUNCATIONS = [
    (19363898.77, 19363898.0),
    (9580894.17, 9580894.0),
    (486007.83, 486007.84),        # rounded UP — truncation is not one-directional
    (46487178.29, 46487180.0),     # the figure from migration 0006's note
    (2133479.8, 2133479.75),
]

# Rate-like values. Fewer digits before the point, more after, same problem.
OBSERVED_RATE_TRUNCATIONS = [
    67.2133333,
    61.95007144978846,
    58.3322,
    96.5649,
]


def as_float32(value: float) -> float:
    """What Postgres `real` stores: the nearest IEEE-754 single-precision value.

    `real` is a 4-byte float, so this is exactly the same rounding — the column
    type and `struct.pack('f')` are the same 24-bit mantissa.
    """
    return struct.unpack("f", struct.pack("f", value))[0]


def as_float64(value: float) -> float:
    """What `double precision` stores. Python floats already are float64."""
    return struct.unpack("d", struct.pack("d", value))[0]


class TestWhatSinglePrecisionDoesToTheseNumbers:
    """Pure arithmetic. Establishes that the figures really are the problem."""

    @pytest.mark.parametrize("value,expected", OBSERVED_TRUNCATIONS)
    def test_a_real_column_cannot_hold_the_value(self, value, expected):
        assert as_float32(value) == pytest.approx(expected, abs=0.01)
        assert as_float32(value) != value

    @pytest.mark.parametrize("value,_expected", OBSERVED_TRUNCATIONS)
    def test_a_double_precision_column_can(self, value, _expected):
        assert as_float64(value) == value

    @pytest.mark.parametrize("rate", OBSERVED_RATE_TRUNCATIONS)
    def test_exchange_rates_lose_digits_too(self, rate):
        # A rate is multiplied by a seven-figure invoice, so a rounding error in
        # the seventh digit of the rate becomes kyats in the total.
        assert as_float32(rate) != rate
        assert as_float64(rate) == rate

    def test_the_error_is_large_enough_to_show_in_an_export(self):
        # 0.77 kyat is not the point. The point is the digits are simply gone:
        # the stored value has no decimal part left to be wrong about.
        stored = as_float32(19363898.77)
        assert stored == int(stored)

    def test_but_far_too_small_for_any_arithmetic_gate_to_notice(self):
        # Why no existing check caught it. The CIF gate's tightest tolerance is
        # 4%; this error is 4e-6 %. No tolerance that permits real-world rounding
        # could ever have flagged this.
        error_pct = abs(as_float32(19363898.77) - 19363898.77) / 19363898.77 * 100
        assert error_pct < 1e-5


class TestTheMigrationsWidenEveryMoneyColumn:
    """Replay the DDL; every money column must survive layer 1.

    Reads the source, not the running database — so it runs in CI with no
    services up, and it fails on the *change* rather than on the deploy.
    Both DDL authors are replayed in boot order (alembic, then the `database.py`
    self-heal fills what no migration creates), because a handful of columns —
    `invoice_price_fc` and `invoice_price_mmk` among them — exist only because of
    the self-heal. `test_schema_contract.py` covers that ownership gap itself.
    """

    def setup_method(self):
        self.declared, self.unparsed = effective_types()

    def test_the_parser_understood_every_statement(self):
        # Guarding this first: a parser that silently skipped a statement would
        # report a clean schema for the wrong reason, which is the exact failure
        # mode of the original defect.
        assert self.unparsed == []

    @pytest.mark.parametrize("table,column", sorted(db_engine.MONEY_COLUMNS))
    def test_money_column_is_not_stored_as_real_or_text(self, table, column):
        declared = self.declared.get((table, column))
        why = db_engine.MONEY_COLUMNS[(table, column)]
        assert declared is not None, (
            f"{table}.{column} is in the money contract but neither the "
            f"migrations nor the database.py self-heal creates it")
        assert declared in db_engine._ACCEPTABLE_MONEY_TYPES, (
            f"{table}.{column} is {declared}; it holds {why}. "
            f"real truncates 19,363,898.77 to 19,363,898.0; text cannot be "
            f"summed or compared numerically.")

    @pytest.mark.parametrize("value,truncated", OBSERVED_TRUNCATIONS)
    def test_the_kyat_line_value_survives_its_declared_type(
            self, value, truncated):
        """The round-trip, simulated at the declared type of the column that broke.

        `items.customs_value_mmk` is the column whose truncated figures reached
        the customer. Simulating its declared storage is the closest a test with
        no database can get to layer 3.

        LIMIT: this reads the base type only, not the precision and scale — the
        parser normalises `numeric(20,4)` to `numeric`, and `numeric` is treated
        as storing the decimal exactly. A money column declared `numeric(9,4)`
        would pass here and still round in production. Only the live layer below,
        which reads `format_type` and includes the typmod, can catch that.
        """
        declared = self.declared.get(("items", "customs_value_mmk"))
        if declared == "numeric":
            stored = value                      # arbitrary-precision decimal
        elif declared == "double precision":
            stored = as_float64(value)
        elif declared == "real":
            stored = as_float32(value)
        else:
            stored = None                       # text: no numeric promise at all
        assert stored == value, (
            f"items.customs_value_mmk is declared {declared}: writing {value!r} "
            f"reads back {stored!r} (a real column gives {truncated!r})")


# ── layer 3: the live database ───────────────────────────────────────────────

def _live_conn():
    """A raw connection, or None when no database is reachable.

    Deliberately quiet and quick: the fork-owned suite runs in ~2 seconds and a
    developer without Postgres up must not pay a connection timeout for it. Set
    `RO_ED_TEST_DB` to point the DB-backed tests somewhere else — same shape as
    `RO_ED_TEST_PDFS` in conftest.py.
    """
    url = os.environ.get("RO_ED_TEST_DB") or db_engine.DATABASE_URL
    try:
        from sqlalchemy import create_engine
        # A private engine, not `db_engine.engine`: the shared one has no connect
        # timeout, so a DATABASE_URL pointing at a host that is merely unreachable
        # (rather than unresolvable) would hang the suite instead of skipping it.
        return create_engine(
            url, connect_args={"connect_timeout": 2}).raw_connection()
    except Exception:
        return None


@pytest.fixture(scope="module")
def live_conn():
    conn = _live_conn()
    if conn is None:
        pytest.skip("no database reachable (set RO_ED_TEST_DB or DATABASE_URL)")
    yield conn
    try:
        conn.rollback()
        conn.close()
    except Exception:
        pass


def _live_column_type(conn, table, column):
    """`format_type` of a live column — `numeric(20,4)`, not just `numeric`.

    The precision and scale matter: `numeric(20,4)` silently rounds a 10-decimal
    exchange rate, so a test that only checked the base type would pass on a
    column that still loses digits.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT format_type(a.atttypid, a.atttypmod) "
        "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = %s "
        "AND a.attname = %s AND a.attnum > 0 AND NOT a.attisdropped",
        (table, column),
    )
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


class TestTheLiveColumnRoundTripsTheRealValues:
    """Write into the type the live column actually has; read it back.

    Uses a TEMP table of the same type rather than the real one — a guard must
    not write to `items`. A temp table is session-scoped and disappears with the
    connection, so this leaves nothing behind even against production.

    This is the layer that catches a migration that exists in the tree but was
    never applied. When it was written, the live database was stamped
    `0006_decl_money_precision` while `0007_storage_types` sat in
    `alembic/versions/` unapplied — the static layer above was green and this one
    was not.
    """

    @pytest.mark.parametrize("table,column", sorted(db_engine.MONEY_COLUMNS))
    def test_the_value_comes_back_unchanged(self, live_conn, table, column):
        col_type = _live_column_type(live_conn, table, column)
        if col_type is None:
            pytest.skip(f"{table}.{column} does not exist in this database")
        if (table, column) in db_engine.PERCENTAGE_COLUMNS:
            # A value probe cannot say anything about these two. They are
            # numeric(9,4) by contract, so a kyat probe overflows before it
            # measures anything — and no value they CAN hold distinguishes
            # correct storage from `real`: at four decimals and a magnitude
            # under 1, float32's shortest representation reads back exactly
            # (0.05 -> '0.05'). A probe that passes on the defect is worse than
            # no probe. Their type and width are covered live by
            # `assert_no_schema_drift`, which rejects `real` outright.
            pytest.skip(f"{table}.{column} is a percentage — covered by "
                        f"assert_no_schema_drift, not by a value probe")

        cur = live_conn.cursor()
        try:
            cur.execute("CREATE TEMP TABLE _precision_probe (v %s) ON COMMIT DROP"
                        % col_type)
            for value, truncated in OBSERVED_TRUNCATIONS:
                cur.execute("DELETE FROM _precision_probe")
                cur.execute("INSERT INTO _precision_probe (v) VALUES (%s)", (value,))
                cur.execute("SELECT v FROM _precision_probe")
                got = float(cur.fetchone()[0])
                assert got == value, (
                    f"{table}.{column} is {col_type}: wrote {value!r}, read back "
                    f"{got!r}. A `real` column gives {truncated!r}.")
        finally:
            cur.close()
            live_conn.rollback()

    @pytest.mark.parametrize("rate", OBSERVED_RATE_TRUNCATIONS)
    def test_the_exchange_rate_keeps_all_its_digits(self, live_conn, rate):
        col_type = _live_column_type(live_conn, "declarations", "exchange_rate")
        if col_type is None:
            pytest.skip("declarations.exchange_rate does not exist")
        cur = live_conn.cursor()
        try:
            cur.execute("CREATE TEMP TABLE _rate_probe (v %s) ON COMMIT DROP"
                        % col_type)
            cur.execute("INSERT INTO _rate_probe (v) VALUES (%s)", (rate,))
            cur.execute("SELECT v FROM _rate_probe")
            assert float(cur.fetchone()[0]) == rate
        finally:
            cur.close()
            live_conn.rollback()
