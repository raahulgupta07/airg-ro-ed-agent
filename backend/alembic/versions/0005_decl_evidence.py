"""Add per-field extraction evidence to declarations.

Every ROVER run produces a `Cell{value, source, confidence, model, status,
alternates, note}` for each declaration field — the engine's own account of how
sure it is and what the challenger said instead. That record was computed on
every job and then discarded at the `_save_to_db` whitelist, so a reviewer had
no way to see *why* a value was flagged. This column stores it as JSON.

Additive + nullable; legacy rows stay NULL and the review payload treats a NULL
as "no evidence available", never as "nothing wrong". Mirrored by a self-heal
ALTER in database.init_database so an un-migrated deploy still gets the column.
"""
from alembic import op

revision = "0005_decl_evidence"
down_revision = "0004_perf_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE declarations ADD COLUMN IF NOT EXISTS evidence_json TEXT")


def downgrade():
    op.execute("ALTER TABLE declarations DROP COLUMN IF EXISTS evidence_json")
