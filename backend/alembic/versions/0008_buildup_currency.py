"""Record the currency each CIF build-up line is printed in.

`freight_value`, `insurance_value` and `adjustment_value` were all assumed to be
in the invoice currency — every prompt said so, and `reconcile.py` does
`(invoice_price + freight + insurance + adjustment) x exchange_rate`.

The declarations disagree. Each line prints its OWN currency code beside the
amount, and they are not always the same one:

    Invoice price      A - C&F - CNY -   82,022.1072      (MMK) 24,307,579.55
    Insurance          E - MMK -        267,383.37
    Adjustment value   AD - CNY -         1,051.894
    Total customs value              24,886,695.86

Insurance there is ALREADY in kyats while Adjustment on the same form is in CNY.
Converting each line from its own unit reproduces the printed total to within
0.05 MMK:

    24,307,579.55 + 267,383.37 + (1,051.894 x 296.354) = 24,886,695.91

Applying the old blanket rule gives 103,859,443 — four times the real value. The
error never surfaced because the extractors were also returning 0 for these lines
(their prompts said "0 if not shown"), so the build-up branch was rarely taken
with real numbers in it. Fixing the prompts without this column would have started
failing the CIF gate on correct documents.

Nullable TEXT: absent means "not stated on the form", and the reader falls back to
the invoice currency exactly as before, so old rows keep their current meaning.
"""
import sqlalchemy as sa
from alembic import op

revision = "0008_buildup_currency"
down_revision = "0007_storage_types"
branch_labels = None
depends_on = None

_COLS = ("freight_currency", "insurance_currency", "adjustment_currency")


def _present() -> set:
    """Columns already on `declarations`, in one query.

    Same guard as 0006/0007: 0006 aborted a first-time deploy by altering a column
    no migration creates, and a rolled-back chain leaves no schema at all.
    """
    rows = op.get_bind().execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'declarations'"
        )
    )
    return {r[0] for r in rows}


def upgrade():
    have = _present()
    for c in _COLS:
        if c not in have:
            op.add_column("declarations", sa.Column(c, sa.Text(), nullable=True))


def downgrade():
    have = _present()
    for c in _COLS:
        if c in have:
            op.drop_column("declarations", c)
