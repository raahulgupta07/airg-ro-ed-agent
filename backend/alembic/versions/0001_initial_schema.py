"""initial schema (full Postgres translation of the legacy SQLite layout)

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-06

This single migration captures every table, column, and index that
`database.init_database()` used to create on the legacy SQLite backend.
All statements use `IF NOT EXISTS` so the migration is idempotent and safe to
run against a partially populated database (e.g. one that's been initialised
by `init_database()` directly).
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


# All 25 tables + indexes. Idempotent (IF NOT EXISTS everywhere).
DDL = [
    # ── jobs ────────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        pdf_name TEXT NOT NULL,
        pdf_hash TEXT NOT NULL,
        pdf_path TEXT,
        pdf_size INTEGER,
        total_pages INTEGER,
        text_pages INTEGER,
        image_pages INTEGER,
        status TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        processing_time_seconds REAL,
        cost_usd REAL,
        accuracy_percent REAL,
        error_message TEXT,
        pdf_storage TEXT DEFAULT 'local',
        user_id INTEGER,
        username TEXT,
        pipeline_version TEXT DEFAULT 'v1',
        cross_validation_json TEXT,
        tokens_in INTEGER DEFAULT 0,
        tokens_out INTEGER DEFAULT 0,
        document_type TEXT,
        pipeline_mode TEXT,
        review_status TEXT DEFAULT 'pending_review',
        reviewed_by TEXT,
        reviewed_at TEXT,
        review_notes TEXT,
        edits_count INTEGER DEFAULT 0,
        parent_job_id TEXT,
        field_bboxes_json TEXT
    )
    """,
    # ── storage_config ──────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS storage_config (
        id SERIAL PRIMARY KEY,
        label TEXT NOT NULL,
        provider TEXT NOT NULL,
        endpoint_url TEXT,
        region_name TEXT,
        bucket_name TEXT,
        access_key_id TEXT,
        secret_access_key_encrypted TEXT,
        key_prefix TEXT DEFAULT '',
        use_ssl INTEGER DEFAULT 1,
        addressing_style TEXT DEFAULT 'auto',
        signature_version TEXT DEFAULT 's3v4',
        use_for_uploads INTEGER DEFAULT 1,
        use_for_exports INTEGER DEFAULT 1,
        use_for_cache INTEGER DEFAULT 0,
        use_for_archive INTEGER DEFAULT 0,
        active INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # ── items ───────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS items (
        id SERIAL PRIMARY KEY,
        job_id TEXT NOT NULL,
        item_name TEXT,
        customs_duty_rate REAL,
        quantity TEXT,
        invoice_unit_price TEXT,
        cif_unit_price TEXT,
        commercial_tax_percent REAL,
        exchange_rate TEXT,
        hs_code TEXT,
        origin_country TEXT,
        customs_value_mmk REAL,
        is_valid INTEGER DEFAULT 1,
        is_deleted INTEGER DEFAULT 0,
        display_order INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # ── declarations ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS declarations (
        id SERIAL PRIMARY KEY,
        job_id TEXT NOT NULL,
        declaration_no TEXT,
        declaration_date TEXT,
        importer_name TEXT,
        consignor_name TEXT,
        invoice_number TEXT,
        invoice_number_customs_declaration TEXT,
        invoice_number_commercial_invoice TEXT,
        invoice_price REAL,
        currency TEXT,
        exchange_rate REAL,
        currency_2 TEXT,
        total_customs_value REAL,
        import_export_customs_duty REAL,
        commercial_tax_ct REAL,
        advance_income_tax_at REAL,
        security_fee_sf REAL,
        maccs_service_fee_mf REAL,
        exemption_reduction REAL,
        document_format TEXT,
        sanity_flags_json TEXT,
        cross_val_passed INTEGER,
        verified INTEGER,
        is_valid INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # ── processing_logs ─────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS processing_logs (
        id SERIAL PRIMARY KEY,
        job_id TEXT NOT NULL,
        step_number INTEGER,
        step_name TEXT,
        status TEXT,
        message TEXT,
        duration_seconds REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # ── pdf_metadata ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS pdf_metadata (
        job_id TEXT PRIMARY KEY,
        pdf_path TEXT,
        metadata_json TEXT
    )
    """,
    # ── users ───────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT,
        role TEXT NOT NULL DEFAULT 'user',
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_login TIMESTAMP,
        keycloak_id TEXT,
        email TEXT,
        default_ldap_id INTEGER,
        ldap_dn TEXT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_keycloak_id ON users(keycloak_id) WHERE keycloak_id IS NOT NULL",
    # ── activity_logs ───────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS activity_logs (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        username TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT,
        ip_address TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        user_agent TEXT,
        auth_source TEXT,
        status TEXT,
        duration_ms INTEGER,
        resource TEXT,
        severity TEXT DEFAULT 'info',
        error_message TEXT,
        payload_json TEXT
    )
    """,
    # ── page_contents ───────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS page_contents (
        id SERIAL PRIMARY KEY,
        job_id TEXT NOT NULL,
        user_id INTEGER,
        pdf_name TEXT,
        page_number INTEGER NOT NULL,
        page_type TEXT,
        source_agent TEXT,
        content TEXT,
        char_count INTEGER DEFAULT 0,
        has_tables INTEGER DEFAULT 0,
        has_numbers INTEGER DEFAULT 0,
        ocr_status TEXT,
        skip INTEGER DEFAULT 0,
        filter_reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        content_tsv tsvector
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_page_contents_tsv ON page_contents USING GIN (content_tsv)",
    # ── settings ────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_by TEXT
    )
    """,
    # ── groups ──────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS groups (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        description TEXT DEFAULT '',
        page_agent INTEGER DEFAULT 1,
        page_history INTEGER DEFAULT 1,
        page_items INTEGER DEFAULT 1,
        page_declarations INTEGER DEFAULT 1,
        page_costs INTEGER DEFAULT 1,
        page_settings INTEGER DEFAULT 0,
        action_run_pipeline INTEGER DEFAULT 1,
        action_upload_pdf INTEGER DEFAULT 1,
        action_download_excel INTEGER DEFAULT 1,
        action_delete_jobs INTEGER DEFAULT 0,
        action_export_data INTEGER DEFAULT 1,
        data_scope TEXT DEFAULT 'own',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # ── user_groups ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS user_groups (
        user_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL,
        assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        assigned_by TEXT,
        PRIMARY KEY (user_id, group_id)
    )
    """,
    # ── page_extractions ────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS page_extractions (
        id SERIAL PRIMARY KEY,
        job_id TEXT NOT NULL,
        page_number INTEGER NOT NULL,
        page_type TEXT,
        language TEXT,
        confidence REAL,
        explanation TEXT,
        doc_title TEXT,
        doc_issuer TEXT,
        doc_date TEXT,
        doc_reference TEXT,
        doc_country TEXT,
        fields_json TEXT,
        items_json TEXT,
        amounts_json TEXT,
        entities_json TEXT,
        has_logo INTEGER DEFAULT 0,
        has_stamp INTEGER DEFAULT 0,
        has_signature INTEGER DEFAULT 0,
        has_barcode INTEGER DEFAULT 0,
        visual_quality TEXT,
        raw_char_count INTEGER DEFAULT 0,
        orientation TEXT,
        pipeline_version TEXT DEFAULT 'v2',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # ── importer_profiles ───────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS importer_profiles (
        id SERIAL PRIMARY KEY,
        importer_name TEXT NOT NULL,
        importer_name_normalized TEXT NOT NULL,
        currency TEXT,
        exchange_rate_min REAL,
        exchange_rate_max REAL,
        exchange_rate_avg REAL,
        common_consignor TEXT,
        common_items TEXT,
        total_jobs INTEGER DEFAULT 0,
        last_job_date TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        fee_baseline_json TEXT
    )
    """,
    # ── field_accuracy ──────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS field_accuracy (
        id SERIAL PRIMARY KEY,
        importer_name_normalized TEXT NOT NULL,
        field_key TEXT NOT NULL,
        total_extractions INTEGER DEFAULT 0,
        corrections_count INTEGER DEFAULT 0,
        last_correction_at TEXT,
        UNIQUE(importer_name_normalized, field_key)
    )
    """,
    # ── value_audit ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS value_audit (
        id SERIAL PRIMARY KEY,
        job_id TEXT NOT NULL,
        table_key TEXT NOT NULL,
        field_key TEXT NOT NULL,
        item_index INTEGER,
        stage TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        source TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # ── corrections ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS corrections (
        id SERIAL PRIMARY KEY,
        job_id TEXT NOT NULL,
        profile_id INTEGER DEFAULT 1,
        table_key TEXT NOT NULL,
        field_key TEXT NOT NULL,
        item_index INTEGER,
        original_value TEXT,
        corrected_value TEXT NOT NULL,
        correction_type TEXT DEFAULT 'wrong_value',
        page_source INTEGER,
        raw_text_context TEXT,
        user_id INTEGER,
        username TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # ── learning_events ─────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS learning_events (
        id SERIAL PRIMARY KEY,
        profile_id INTEGER DEFAULT 1,
        event_type TEXT NOT NULL,
        event_data TEXT,
        trigger_correction_id INTEGER,
        corrections_analyzed INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # ── ldap_configs ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ldap_configs (
        id SERIAL PRIMARY KEY,
        label TEXT NOT NULL,
        host TEXT NOT NULL,
        port INTEGER NOT NULL,
        use_tls INTEGER DEFAULT 0,
        validate_cert INTEGER DEFAULT 0,
        bind_dn TEXT,
        bind_password_encrypted TEXT,
        search_base TEXT,
        search_filter TEXT,
        attr_username TEXT DEFAULT 'uid',
        attr_mail TEXT DEFAULT 'mail',
        attr_groups TEXT DEFAULT 'memberOf',
        email_domain_hint TEXT,
        priority INTEGER DEFAULT 50,
        active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # ── field_edits ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS field_edits (
        id SERIAL PRIMARY KEY,
        job_id TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_index INTEGER DEFAULT 0,
        field_name TEXT NOT NULL,
        original_value TEXT,
        corrected_value TEXT,
        edited_by TEXT,
        edited_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
        pdf_page_ref INTEGER,
        reason TEXT
    )
    """,

    # ── indexes ─────────────────────────────────────────────────────────────
    "CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_items_job ON items(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_items_job_order ON items(job_id, is_deleted, display_order, id)",
    "CREATE INDEX IF NOT EXISTS idx_declarations_job ON declarations(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_logs_job ON processing_logs(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
    "CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_logs(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_activity_user ON activity_logs(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_pages_job ON page_contents(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_pages_user ON page_contents(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_pages_pdf ON page_contents(pdf_name)",
    "CREATE INDEX IF NOT EXISTS idx_page_ext_job ON page_extractions(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_page_ext_type ON page_extractions(page_type)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_importer_normalized ON importer_profiles(importer_name_normalized)",
    "CREATE INDEX IF NOT EXISTS idx_corrections_job ON corrections(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_corrections_profile ON corrections(profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_corrections_field ON corrections(table_key, field_key)",
    "CREATE INDEX IF NOT EXISTS idx_field_edits_job ON field_edits(job_id)",
]


def upgrade() -> None:
    for stmt in DDL:
        op.execute(stmt)


def downgrade() -> None:
    for tbl in (
        "field_edits", "ldap_configs", "learning_events", "corrections",
        "value_audit", "field_accuracy", "importer_profiles",
        "page_extractions", "user_groups", "groups", "settings",
        "page_contents", "activity_logs", "users", "pdf_metadata",
        "processing_logs", "declarations", "items", "storage_config", "jobs",
    ):
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
