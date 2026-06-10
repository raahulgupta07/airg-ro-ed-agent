"""Add CIF build-up fields to declarations (freight / insurance / adjustment).

Customs value (MMK) ≈ (invoice_price + freight + insurance + adjustment) × rate.
Capturing the build-up lets the Atlas reconcile CIF gate tighten (it no longer
has to absorb unexplained freight/insurance as slack). All nullable + additive;
legacy rows stay NULL. Mirrored by a self-heal ALTER in database.init_database.
"""
from alembic import op

revision = "0003_decl_cif_fields"
down_revision = "0002_jobs_model_used"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE declarations ADD COLUMN IF NOT EXISTS freight_value REAL")
    op.execute("ALTER TABLE declarations ADD COLUMN IF NOT EXISTS insurance_value REAL")
    op.execute("ALTER TABLE declarations ADD COLUMN IF NOT EXISTS adjustment_value REAL")


def downgrade():
    op.execute("ALTER TABLE declarations DROP COLUMN IF EXISTS adjustment_value")
    op.execute("ALTER TABLE declarations DROP COLUMN IF EXISTS insurance_value")
    op.execute("ALTER TABLE declarations DROP COLUMN IF EXISTS freight_value")
