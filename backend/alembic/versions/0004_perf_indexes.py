"""Performance indexes for the hottest query paths.

- jobs(review_status, created_at DESC)   review queue list (WHERE review_status ORDER BY created_at)
- jobs(user_id, created_at DESC)         per-user job history
- items(job_id) WHERE active             items_count subquery / item lists (partial, skips soft-deleted)
- activity_logs(username, created_at)    activity page username filter

All IF NOT EXISTS + additive; mirrored by self-heal in database.init_database.
"""
from alembic import op

revision = "0004_perf_indexes"
down_revision = "0003_decl_cif_fields"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_review_created "
        "ON jobs(review_status, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_user_created "
        "ON jobs(user_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_items_job_active "
        "ON items(job_id) WHERE COALESCE(is_deleted, 0) = 0"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_username_created "
        "ON activity_logs(username, created_at DESC)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_activity_username_created")
    op.execute("DROP INDEX IF EXISTS idx_items_job_active")
    op.execute("DROP INDEX IF EXISTS idx_jobs_user_created")
    op.execute("DROP INDEX IF EXISTS idx_jobs_review_created")
