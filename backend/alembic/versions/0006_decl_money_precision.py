"""Widen the declaration money columns from `real` to `double precision`.

Postgres `real` is single precision: about 7 significant decimal digits. A Myanmar
customs value routinely needs 10 — 46,487,178.29 is stored as 46,487,180.0, and
an exchange rate of 67.2133333 becomes 67.21333. Every write silently rounded.

Three consequences, all real:
  * the CIF identity cannot close in the DB even when it closed exactly at
    extraction time, so correct documents look suspect;
  * every Excel export has been shipping rounded totals;
  * a reviewer correcting a value gets "saved", an audit row recording the fix,
    and no change to the stored number — the correction is rounded straight back
    off. That last one was found by resolving a field through the checks API and
    reading the column afterwards.

`real` -> `double precision` is a widening cast, so this is safe and needs no
data migration. It does NOT restore digits already lost on existing rows: those
values were truncated on write and the information is gone. Re-run affected
documents to recover exact figures.
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_decl_money_precision"
down_revision = "0005_decl_evidence"
branch_labels = None
depends_on = None

_COLS = [
    "invoice_price", "invoice_price_fc", "invoice_price_mmk", "exchange_rate",
    "freight_value", "insurance_value", "adjustment_value",
    "total_customs_value", "import_export_customs_duty",
    "commercial_tax_ct", "advance_income_tax_at",
    "security_fee_sf", "maccs_service_fee_mf", "exemption_reduction",
]


def _present_cols():
    """The subset of `_COLS` that actually exists on `declarations`.

    Not every column above is created by a migration: `invoice_price_fc` and
    `invoice_price_mmk` are added by the self-heal ALTERs in `database.py`, which
    run at app startup — i.e. AFTER alembic. On a fresh database they are absent
    when this migration runs, and an unconditional ALTER aborts the whole
    transactional upgrade, rolling back every migration and leaving no schema at
    all. Widen what is here; the self-heal creates the rest at the right width.

    One query, filtered to the same schema the unqualified ALTER below resolves to.
    """
    rows = op.get_bind().execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'declarations'"
        )
    )
    have = {r[0] for r in rows}
    return [c for c in _COLS if c in have]


def upgrade():
    for c in _present_cols():
        op.execute(f"ALTER TABLE declarations ALTER COLUMN {c} TYPE double precision")


def downgrade():
    # Narrowing back to `real` would re-round every value. Kept for completeness;
    # running it loses precision again.
    for c in _present_cols():
        op.execute(f"ALTER TABLE declarations ALTER COLUMN {c} TYPE real")
