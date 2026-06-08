"""add model_used + processed_at to jobs

Revision ID: 0002_jobs_model_used
Revises: 0001_initial
Create Date: 2026-05-27
"""
from alembic import op


revision = "0002_jobs_model_used"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS model_used VARCHAR(100)")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS processed_at TIMESTAMP")


def downgrade():
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS processed_at")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS model_used")
