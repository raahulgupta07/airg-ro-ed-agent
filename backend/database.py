#!/usr/bin/env python3
"""
PostgreSQL-backed Database for PDF Extraction Jobs
(formerly SQLite — see db_engine.py for the compat shim that keeps the raw
SQL in this file working with psycopg3.)

Tracks all processing jobs with full history.
"""

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
import hashlib
import bcrypt

# psycopg-backed pooled connections, with a sqlite3-compatible facade so the
# 60+ functions below didn't have to be rewritten.
import db_engine
from db_engine import (
    OperationalError,
    IntegrityError,
    ProgrammingError,
    DatabaseError,
)


# Backwards-compat: a number of call sites do `import sqlite3` and use
# `sqlite3.OperationalError` / `sqlite3.Row` / `sqlite3.IntegrityError`. Keep
# a tiny shim module so they continue to work.
class _SqliteShim:
    Row = object  # truthy sentinel — _Sqlite3CompatConnection treats any value as 'dict rows'
    OperationalError = OperationalError
    IntegrityError = IntegrityError
    ProgrammingError = ProgrammingError
    DatabaseError = DatabaseError


sqlite3 = _SqliteShim()  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Legacy file path — only used by the one-shot SQLite→Postgres migrator.
DB_PATH = Path(__file__).parent / "data" / "extraction_history.db"

# Connection timeout (seconds) — handled by SQLAlchemy QueuePool now.
DB_TIMEOUT = 30.0


def _connect():
    """Pooled Postgres connection wrapped in a sqlite3-compatible facade.

    The facade auto-translates `?`→`%s`, `INSERT OR REPLACE`→`ON CONFLICT`,
    `PRAGMA …` → no-op, and exposes `cursor.lastrowid` (via RETURNING id).
    """
    return db_engine.get_conn()

_INIT_DONE = False

def init_database():
    """Initialize Postgres schema (idempotent + per-process guard + advisory lock).

    Multiple uvicorn workers can call this in parallel; we use a Postgres advisory
    lock so only one runs Alembic+DDL at a time, others wait then no-op.
    """
    global _INIT_DONE
    if _INIT_DONE:
        return

    # Ensure local data dir still exists for non-DB artefacts (PDFs, exports).
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Acquire a Postgres advisory lock — serializes init across workers.
    _lock_conn = None
    try:
        _lock_conn = _connect()
        _lc = _lock_conn.cursor()
        _lc.execute("SELECT pg_advisory_lock(823651749)")  # arbitrary 64-bit key
        _lock_conn.commit()
    except Exception as exc:
        logger.debug("init advisory lock skipped: %s", exc)
        _lock_conn = None

    try:
        db_engine.run_alembic_upgrade()
    except Exception as exc:  # pragma: no cover — non-fatal
        logger.debug("alembic upgrade skipped: %s", exc)

    conn = _connect()
    cursor = conn.cursor()

    # Jobs table — one row per PDF processed
    cursor.execute("""
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
            error_message TEXT
        )
    """)
    conn.commit()

    cursor.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS pdf_path TEXT")
    cursor.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS pdf_storage TEXT DEFAULT 'local'")
    conn.commit()

    # Storage config table — S3/GCS/Azure provider configuration
    cursor.execute("""
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
    """)

    # Items table - extracted data items FORMAT 1 (6 fields - linked to jobs)
    cursor.execute("""
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        )
    """)

    # Declaration table - extracted data FORMAT 2 (16 fields - linked to jobs)
    cursor.execute("""
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
            is_valid INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        )
    """)

    # Processing logs - detailed step logs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processing_logs (
            id SERIAL PRIMARY KEY,
            job_id TEXT NOT NULL,
            step_number INTEGER,
            step_name TEXT,
            status TEXT,
            message TEXT,
            duration_seconds REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        )
    """)

    # PDF metadata - store full PDF info
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pdf_metadata (
            job_id TEXT PRIMARY KEY,
            pdf_path TEXT,
            metadata_json TEXT,
            FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        )
    """)

    # Users table — authentication and role management
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            role TEXT NOT NULL DEFAULT 'user',
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )
    """)

    # Activity logs — tracks who did what
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            ip_address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Page contents — stores raw text per page for search/RAG
    cursor.execute("""
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
            FOREIGN KEY (job_id) REFERENCES jobs(job_id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Postgres equivalent of FTS5: add a tsvector column + GIN index. Search
    # falls back to ILIKE if the column is absent (see search_page_contents).
    cursor.execute(
        "ALTER TABLE page_contents ADD COLUMN IF NOT EXISTS "
        "content_tsv tsvector"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_page_contents_tsv "
        "ON page_contents USING GIN (content_tsv)"
    )
    conn.commit()

    # Add user_id to jobs (idempotent in Postgres via IF NOT EXISTS)
    cursor.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS user_id INTEGER")
    cursor.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS username TEXT")
    conn.commit()

    # Settings table — key-value store for app configuration
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by TEXT
        )
    """)

    # Keycloak migration: add keycloak_id and email to users (idempotent)
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS keycloak_id TEXT")
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_keycloak_id "
        "ON users(keycloak_id) WHERE keycloak_id IS NOT NULL"
    )
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT")
    conn.commit()

    # Create default admin if no users exist
    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        import os
        default_user = os.getenv("ADMIN_DEFAULT_USERNAME", "admin")
        default_pw = os.getenv("ADMIN_DEFAULT_PASSWORD", "admin123")
        admin_hash = bcrypt.hashpw(default_pw.encode(), bcrypt.gensalt()).decode()
        cursor.execute("""
            INSERT INTO users (username, password_hash, display_name, role)
            VALUES (?, ?, 'Administrator', 'admin')
        """, (default_user, admin_hash))
        logger.info(f"Created default admin user: {default_user}")

    # Groups table — RBAC permission groups
    cursor.execute("""
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
    """)

    # User-group assignments (many-to-many)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_groups (
            user_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            assigned_by TEXT,
            PRIMARY KEY (user_id, group_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE
        )
    """)

    # Create default "Users" group if no groups exist
    cursor.execute("SELECT COUNT(*) FROM groups")
    if cursor.fetchone()[0] == 0:
        cursor.execute("""
            INSERT INTO groups (name, description, page_agent, page_history, page_items,
                page_declarations, page_costs, page_settings, action_run_pipeline,
                action_upload_pdf, action_download_excel, action_delete_jobs,
                action_export_data, data_scope)
            VALUES ('Users', 'Default group for all users', 1, 1, 1, 1, 1, 0, 1, 1, 1, 0, 1, 'own')
        """)
        logger.info("Created default 'Users' group (no settings access, no delete)")

    # Page extractions table — v2 per-page structured data
    cursor.execute("""
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_page_ext_job ON page_extractions(job_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_page_ext_type ON page_extractions(page_type)")

    # Idempotent jobs-table column adds (Postgres native IF NOT EXISTS)
    for stmt in (
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS pipeline_version TEXT DEFAULT 'v1'",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cross_validation_json TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tokens_in INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tokens_out INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS document_type TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS pipeline_mode TEXT",
    ):
        cursor.execute(stmt)
    conn.commit()

    # Importer profiles — learned patterns per importer
    cursor.execute("""
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
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute(
        "ALTER TABLE importer_profiles ADD COLUMN IF NOT EXISTS fee_baseline_json TEXT"
    )
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_importer_normalized "
        "ON importer_profiles(importer_name_normalized)"
    )
    conn.commit()

    # Field accuracy tracker — which fields fail per importer
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS field_accuracy (
            id SERIAL PRIMARY KEY,
            importer_name_normalized TEXT NOT NULL,
            field_key TEXT NOT NULL,
            total_extractions INTEGER DEFAULT 0,
            corrections_count INTEGER DEFAULT 0,
            last_correction_at TEXT,
            UNIQUE(importer_name_normalized, field_key)
        )
    """)

    # Value audit trail — tracks every change to an extracted value
    cursor.execute("""
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        )
    """)

    # Create indexes for faster queries
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_job ON items(job_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_declarations_job ON declarations(job_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_logs_job ON processing_logs(job_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_logs(created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_user ON activity_logs(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pages_job ON page_contents(job_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pages_user ON page_contents(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pages_pdf ON page_contents(pdf_name)")

    # Clean up stale PROCESSING jobs from previous crashes/restarts
    cursor.execute("""
        UPDATE jobs SET status = 'FAILED', error_message = 'Server restarted during processing'
        WHERE status = 'PROCESSING'
    """)
    stale = cursor.rowcount
    if stale:
        logger.info(f"Cleaned up {stale} stale PROCESSING job(s)")

    # Idempotent column adds (Postgres native IF NOT EXISTS)
    for stmt in (
        "ALTER TABLE items ADD COLUMN IF NOT EXISTS hs_code TEXT",
        "ALTER TABLE items ADD COLUMN IF NOT EXISTS origin_country TEXT",
        "ALTER TABLE items ADD COLUMN IF NOT EXISTS customs_value_mmk REAL",
        "ALTER TABLE items ADD COLUMN IF NOT EXISTS cif_unit_price TEXT",
        # V11 Review UI: soft-delete + display order for items table
        "ALTER TABLE items ADD COLUMN IF NOT EXISTS is_deleted INTEGER DEFAULT 0",
        "ALTER TABLE items ADD COLUMN IF NOT EXISTS display_order INTEGER DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS idx_items_job_order "
        "ON items(job_id, is_deleted, display_order, id)",
        "ALTER TABLE declarations ADD COLUMN IF NOT EXISTS invoice_number_customs_declaration TEXT",
        # CUSDEC-1 / handwritten-doc support
        "ALTER TABLE declarations ADD COLUMN IF NOT EXISTS document_format TEXT",
        "ALTER TABLE declarations ADD COLUMN IF NOT EXISTS sanity_flags_json TEXT",
        "ALTER TABLE declarations ADD COLUMN IF NOT EXISTS cross_val_passed INTEGER",
        "ALTER TABLE declarations ADD COLUMN IF NOT EXISTS verified INTEGER",
        "ALTER TABLE declarations ADD COLUMN IF NOT EXISTS invoice_number_commercial_invoice TEXT",
    ):
        cursor.execute(stmt)
    conn.commit()

    # Corrections table — stores user corrections for self-learning
    cursor.execute("""
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
    """)

    # Learning events — audit trail for auto-generated rules
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS learning_events (
            id SERIAL PRIMARY KEY,
            profile_id INTEGER DEFAULT 1,
            event_type TEXT NOT NULL,
            event_data TEXT,
            trigger_correction_id INTEGER,
            corrections_analyzed INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Indexes for corrections
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_corrections_job ON corrections(job_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_corrections_profile ON corrections(profile_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_corrections_field ON corrections(table_key, field_key)")

    # ─── Multi-LDAP support ───────────────────────────────
    cursor.execute("""
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
    """)

    # User + activity_logs + jobs idempotent column adds
    for stmt in (
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_ldap_id INTEGER",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS ldap_dn TEXT",
        "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS ip_address TEXT",
        "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS user_agent TEXT",
        "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS auth_source TEXT",
        "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS status TEXT",
        "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS duration_ms INTEGER",
        "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS resource TEXT",
        "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS severity TEXT DEFAULT 'info'",
        "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS error_message TEXT",
        "ALTER TABLE activity_logs ADD COLUMN IF NOT EXISTS payload_json TEXT",
        # ── Review/Approve workflow (V11 Maestro) ──
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS review_status TEXT DEFAULT 'pending_review'",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS reviewed_by TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS reviewed_at TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS review_notes TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS edits_count INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS parent_job_id TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS field_bboxes_json TEXT",
    ):
        cursor.execute(stmt)
    conn.commit()

    # Backfill review_status='pending_review' for existing COMPLETED jobs
    cursor.execute("""
        UPDATE jobs SET review_status = 'pending_review'
        WHERE status = 'COMPLETED' AND (review_status IS NULL OR review_status = '')
    """)
    conn.commit()

    # field_edits table — audit trail per field edit (review workflow)
    cursor.execute("""
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
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_field_edits_job ON field_edits(job_id)")

    conn.commit()
    conn.close()

    # Release advisory lock + mark initialized
    if _lock_conn is not None:
        try:
            _lc = _lock_conn.cursor()
            _lc.execute("SELECT pg_advisory_unlock(823651749)")
            _lock_conn.commit()
            _lock_conn.close()
        except Exception:
            pass
    _INIT_DONE = True
    print(f"✅ Database initialized (postgres) — DSN: {db_engine.DATABASE_URL}")


def insert_activity_log_v2(timestamp, user, action, details=None,
                            ip_address=None, user_agent=None, auth_source=None,
                            status='OK', duration_ms=None, resource=None,
                            severity='info', error_message=None, payload_json=None):
    """V2 activity log insert with all enrichment fields.

    Maps to real `activity_logs` columns: username, detail, created_at.
    """
    # Default username to '-' so NOT NULL constraint never blocks (system events).
    if user in (None, ''):
        user = '-'
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO activity_logs
                (created_at, username, action, detail, ip_address, user_agent,
                 auth_source, status, duration_ms, resource, severity,
                 error_message, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (timestamp, user, action, details, ip_address, user_agent,
              auth_source, status, duration_ms, resource, severity,
              error_message, payload_json))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_activity_log_v2(limit=100, offset=0, action=None, status=None,
                          user=None, severity=None, date_from=None, date_to=None,
                          search=None, action_in=None):
    """Filtered list. action_in: list[str] | None — match if action IN list."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    where = []
    params = []
    if action:
        where.append("action = ?")
        params.append(action)
    if action_in:
        if isinstance(action_in, str):
            action_in = [a.strip() for a in action_in.split(",") if a.strip()]
        if action_in:
            placeholders = ",".join(["?"] * len(action_in))
            where.append(f"action IN ({placeholders})")
            params += list(action_in)
    if status:
        where.append("status = ?")
        params.append(status)
    if user:
        where.append("username = ?")
        params.append(user)
    if severity:
        where.append("severity = ?")
        params.append(severity)
    if date_from:
        where.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("created_at <= ?")
        params.append(date_to)
    if search:
        where.append("(detail ILIKE ? OR resource ILIKE ? OR error_message ILIKE ? OR username ILIKE ?)")
        s = f"%{search}%"
        params += [s, s, s, s]
    sql = ("SELECT id, username AS user, action, detail AS details, "
           "ip_address, user_agent, auth_source, status, duration_ms, "
           "resource, severity, error_message, payload_json, "
           "created_at AS timestamp FROM activity_logs")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def count_activity_log_v2(**filters):
    """Same filters as list, returns total count."""
    conn = _connect()
    cur = conn.cursor()
    where = []
    params = []
    if filters.get("action"):
        where.append("action = ?")
        params.append(filters["action"])
    if filters.get("action_in"):
        ain = filters["action_in"]
        if isinstance(ain, str):
            ain = [a.strip() for a in ain.split(",") if a.strip()]
        if ain:
            placeholders = ",".join(["?"] * len(ain))
            where.append(f"action IN ({placeholders})")
            params += list(ain)
    if filters.get("status"):
        where.append("status = ?")
        params.append(filters["status"])
    if filters.get("user"):
        where.append("username = ?")
        params.append(filters["user"])
    if filters.get("severity"):
        where.append("severity = ?")
        params.append(filters["severity"])
    if filters.get("date_from"):
        where.append("created_at >= ?")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        where.append("created_at <= ?")
        params.append(filters["date_to"])
    if filters.get("search"):
        where.append("(detail ILIKE ? OR resource ILIKE ? OR error_message ILIKE ? OR username ILIKE ?)")
        s = f"%{filters['search']}%"
        params += [s, s, s, s]
    sql = "SELECT COUNT(*) FROM activity_logs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    cur.execute(sql, params)
    n = cur.fetchone()[0]
    conn.close()
    return n


def activity_log_stats(date_from=None, date_to=None):
    """Aggregate stats: total, today, failed_logins, unique_users, top_action."""
    conn = _connect()
    cur = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")

    cur.execute("SELECT COUNT(*) FROM activity_logs")
    total = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM activity_logs WHERE created_at LIKE ?", (f"{today}%",))
    today_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM activity_logs WHERE action = 'LOGIN_FAILED'")
    failed_logins = cur.fetchone()[0]

    cur.execute("SELECT COUNT(DISTINCT username) FROM activity_logs WHERE username IS NOT NULL")
    unique_users = cur.fetchone()[0]

    cur.execute("SELECT action, COUNT(*) c FROM activity_logs GROUP BY action ORDER BY c DESC LIMIT 1")
    top = cur.fetchone()
    top_action = top[0] if top else "N/A"

    cur.execute("""SELECT
                    SUM(CASE WHEN action LIKE 'JOB_%' THEN 1 ELSE 0 END) AS jobs_total,
                    SUM(CASE WHEN action = 'JOB_FAIL' THEN 1 ELSE 0 END) AS jobs_fail
                  FROM activity_logs""")
    j = cur.fetchone()
    conn.close()

    return {
        "total": total, "today": today_count,
        "failed_logins": failed_logins, "unique_users": unique_users,
        "top_action": top_action,
        "jobs_total": j[0] or 0, "jobs_fail": j[1] or 0,
    }

def generate_job_id(pdf_name: str) -> str:
    """Generate unique job ID"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"JOB_{timestamp}_{pdf_name[:20].replace(' ', '_')}"

def calculate_pdf_hash(pdf_path: str) -> str:
    """Calculate SHA256 hash of PDF file for duplicate detection."""
    try:
        with open(pdf_path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception as e:
        logger.warning(f"PDF hash calculation failed: {e}")
        return ""

def create_job(pdf_name: str, pdf_path: str, pdf_size: int, total_pages: int,
               text_pages: int, image_pages: int, user_id: int = None, username: str = None) -> str:
    """Create a new processing job linked to a user"""

    job_id = generate_job_id(pdf_name)
    pdf_hash = calculate_pdf_hash(pdf_path)

    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO jobs (job_id, pdf_name, pdf_hash, pdf_path, pdf_size, total_pages,
                         text_pages, image_pages, status, user_id, username)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PROCESSING', ?, ?)
    """, (job_id, pdf_name, pdf_hash, pdf_path, pdf_size, total_pages, text_pages, image_pages, user_id, username))

    conn.commit()
    conn.close()

    print(f"✅ Created job: {job_id}")
    return job_id

def update_job_pdf_storage(job_id: str, storage_path: str) -> bool:
    """Update the pdf_storage reference for a job (e.g. 's3:uploads/foo.pdf' or 'local')."""
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("UPDATE jobs SET pdf_storage = ? WHERE job_id = ?", (storage_path, job_id))
        conn.commit()
        n = cur.rowcount
        conn.close()
        return n > 0
    except Exception as e:
        logger.warning(f"update_job_pdf_storage failed: {e}")
        return False


def update_job_status(job_id: str, status: str, error_message: str = None):
    """Update job status"""

    conn = _connect()
    cursor = conn.cursor()

    if status == 'COMPLETED':
        cursor.execute("""
            UPDATE jobs
            SET status = ?, completed_at = CURRENT_TIMESTAMP, error_message = ?
            WHERE job_id = ?
        """, (status, error_message, job_id))
    else:
        cursor.execute("""
            UPDATE jobs
            SET status = ?, error_message = ?
            WHERE job_id = ?
        """, (status, error_message, job_id))

    conn.commit()
    conn.close()

def update_job_metrics(job_id: str, processing_time: float, cost: float, accuracy: float):
    """Update job metrics"""

    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE jobs
        SET processing_time_seconds = ?, cost_usd = ?, accuracy_percent = ?
        WHERE job_id = ?
    """, (processing_time, cost, accuracy, job_id))

    conn.commit()
    conn.close()

def update_job_usage(job_id: str, tokens_in: int = 0, tokens_out: int = 0,
                     document_type: str = None, pipeline_mode: str = None):
    """Update token + doc-type + pipeline-mode fields on an existing job."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE jobs
        SET tokens_in = ?, tokens_out = ?, document_type = ?, pipeline_mode = ?
        WHERE job_id = ?
    """, (tokens_in or 0, tokens_out or 0, document_type, pipeline_mode, job_id))
    conn.commit()
    conn.close()

def save_items(job_id: str, items: List[Dict]):
    """Save extracted items to database (sets display_order sequentially)."""

    conn = _connect()
    cursor = conn.cursor()

    # Detect if display_order column exists for forward/backward compatibility
    has_display_order = False
    try:
        cursor.execute("PRAGMA table_info(items)")
        cols = {r[1] for r in cursor.fetchall()}
        has_display_order = 'display_order' in cols
    except Exception:
        has_display_order = False

    def _g(it, *keys, default=''):
        for k in keys:
            v = it.get(k)
            if v not in (None, ''):
                return v
        return default
    for idx, item in enumerate(items):
        v_name      = _g(item, 'item_name', 'Item name')
        v_duty_rate = _g(item, 'customs_duty_rate', 'Customs duty rate', default=0.0)
        v_qty       = _g(item, 'quantity', 'Quantity (1)')
        v_inv_price = _g(item, 'invoice_unit_price', 'Invoice unit price')
        v_cif_price = _g(item, 'cif_unit_price', 'CIF unit price')
        v_tax_pct   = _g(item, 'commercial_tax_percent', 'commercial_tax_pct', 'Commercial tax %', default=0.0)
        v_fx        = _g(item, 'exchange_rate', 'Exchange Rate (1)')
        v_hs        = _g(item, 'hs_code', 'HS Code')
        v_origin    = _g(item, 'origin_country', 'origin', 'Origin Country')
        v_mmk       = _g(item, 'customs_value_mmk', 'Customs Value (MMK)', default=0.0)
        if has_display_order:
            cursor.execute("""
                INSERT INTO items (job_id, item_name, customs_duty_rate, quantity,
                                 invoice_unit_price, cif_unit_price, commercial_tax_percent,
                                 exchange_rate, hs_code, origin_country, customs_value_mmk,
                                 display_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (job_id, v_name, v_duty_rate, v_qty, v_inv_price, v_cif_price,
                  v_tax_pct, v_fx, v_hs, v_origin, v_mmk, idx))
        else:
            cursor.execute("""
                INSERT INTO items (job_id, item_name, customs_duty_rate, quantity,
                                 invoice_unit_price, cif_unit_price, commercial_tax_percent,
                                 exchange_rate, hs_code, origin_country, customs_value_mmk)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (job_id, v_name, v_duty_rate, v_qty, v_inv_price, v_cif_price,
                  v_tax_pct, v_fx, v_hs, v_origin, v_mmk))

    conn.commit()
    conn.close()

    print(f"✅ Saved {len(items)} items for job {job_id}")

def save_declarations(job_id: str, declarations: List[Dict]):
    """Save extracted declarations (Format 2) to database"""

    conn = _connect()
    cursor = conn.cursor()

    def _g(d, *keys, default=''):
        for k in keys:
            v = d.get(k)
            if v not in (None, ''):
                return v
        return default
    for decl in declarations:
        v_no       = _g(decl, 'declaration_no', 'Declaration No')
        v_date     = _g(decl, 'declaration_date', 'Declaration Date')
        v_importer = _g(decl, 'importer_name', 'Importer (Name)')
        v_consign  = _g(decl, 'consignor_name', 'Consignor (Name)')
        v_inv_cd   = _g(decl, 'invoice_number_customs_declaration', 'invoice_number_customs', 'Invoice Number (Customs Declaration)')
        v_inv_ci   = _g(decl, 'invoice_number_commercial_invoice', 'invoice_number_commercial', 'Invoice Number (Commercial Invoice)')
        v_inv_no   = _g(decl, 'invoice_number', 'Invoice Number') or v_inv_ci or v_inv_cd
        v_price    = _g(decl, 'invoice_price', 'Invoice Price', default=0.0)
        v_curr     = _g(decl, 'currency', 'Currency')
        v_rate     = _g(decl, 'exchange_rate', 'Exchange Rate', default=0.0)
        v_curr2    = _g(decl, 'currency_2', 'Currency 2', default=v_curr)
        v_cust_val = _g(decl, 'total_customs_value', 'Total Customs Value', default=0.0)
        v_duty     = _g(decl, 'import_export_customs_duty', 'customs_duty', 'Import/Export Customs Duty', default=0.0)
        v_ct       = _g(decl, 'commercial_tax_ct', 'commercial_tax', 'Commercial Tax (CT)', default=0.0)
        v_at       = _g(decl, 'advance_income_tax_at', 'advance_income_tax', 'Advance Income Tax (AT)', default=0.0)
        v_sf       = _g(decl, 'security_fee_sf', 'security_fee', 'Security Fee (SF)', default=0.0)
        v_mf       = _g(decl, 'maccs_service_fee_mf', 'maccs_service_fee', 'MACCS Service Fee (MF)', default=0.0)
        v_exempt   = _g(decl, 'exemption_reduction', 'exemption', 'Exemption/Reduction', default=0.0)
        cursor.execute("""
            INSERT INTO declarations (
                job_id, declaration_no, declaration_date, importer_name, consignor_name,
                invoice_number, invoice_number_customs_declaration, invoice_number_commercial_invoice,
                invoice_price, currency, exchange_rate, currency_2,
                total_customs_value, import_export_customs_duty, commercial_tax_ct,
                advance_income_tax_at, security_fee_sf, maccs_service_fee_mf, exemption_reduction
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job_id, v_no, v_date, v_importer, v_consign,
            v_inv_no, v_inv_cd, v_inv_ci,
            v_price, v_curr, v_rate, v_curr2,
            v_cust_val, v_duty, v_ct, v_at, v_sf, v_mf, v_exempt
        ))
        new_decl_id = cursor.lastrowid
        # Persist CUSDEC-1 metadata if present (additive — won't fail if columns missing)
        try:
            doc_fmt = decl.get('_document_format') or decl.get('document_format')
            sanity_flags_json = decl.get('_sanity_flags_json')
            cross_val_passed = decl.get('_cross_val_passed')
            verified_flag = decl.get('_verified')
            cv_int = None if cross_val_passed is None else (1 if cross_val_passed else 0)
            ver_int = None if verified_flag is None else (1 if verified_flag else 0)
            cursor.execute("""
                UPDATE declarations SET document_format = ?, sanity_flags_json = ?,
                    cross_val_passed = ?, verified = ? WHERE id = ?
            """, (doc_fmt, sanity_flags_json, cv_int, ver_int, new_decl_id))
        except Exception:
            pass

    conn.commit()
    conn.close()

    print(f"✅ Saved {len(declarations)} declarations for job {job_id}")

def save_pdf_metadata(job_id: str, metadata: Dict):
    """Save PDF metadata JSON"""

    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT OR REPLACE INTO pdf_metadata (job_id, pdf_path, metadata_json)
        VALUES (?, ?, ?)
    """, (job_id, metadata.get('pdf_path', ''), json.dumps(metadata)))

    conn.commit()
    conn.close()

def log_processing_step(job_id: str, step_number: int, step_name: str,
                       status: str, message: str = "", duration: float = 0.0):
    """Log a processing step"""

    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO processing_logs (job_id, step_number, step_name, status, message, duration_seconds)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (job_id, step_number, step_name, status, message, duration))

    conn.commit()
    conn.close()

def get_all_jobs(limit: int = 50) -> List[Dict]:
    """Get all jobs (most recent first)"""

    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM jobs
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))

    jobs = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return jobs

def get_job_items(job_id: str) -> List[Dict]:
    """Get all items for a job (excludes soft-deleted, ordered by display_order then id)."""

    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM items
        WHERE job_id = ? AND COALESCE(is_deleted, 0) = 0
        ORDER BY COALESCE(display_order, 0) ASC, id ASC
    """, (job_id,))

    items = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return items


# Alias used by V11 review code paths.
def list_items(job_id: str) -> List[Dict]:
    """List items for a job (display-order aware, excludes soft-deleted)."""
    return get_job_items(job_id)

def get_job_declarations(job_id: str) -> List[Dict]:
    """Get all declarations for a job (Format 2)"""

    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM declarations
        WHERE job_id = ?
        ORDER BY id
    """, (job_id,))

    declarations = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return declarations

def get_job_logs(job_id: str) -> List[Dict]:
    """Get processing logs for a job"""

    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM processing_logs
        WHERE job_id = ?
        ORDER BY step_number
    """, (job_id,))

    logs = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return logs

def get_job_details(job_id: str) -> Optional[Dict]:
    """Get complete job details"""

    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get job info
    cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    job = cursor.fetchone()

    if not job:
        conn.close()
        return None

    job_dict = dict(job)
    # Postgres returns datetime objects; serialize to strings for Pydantic compat
    import datetime as _dt
    for _k, _v in list(job_dict.items()):
        if isinstance(_v, (_dt.datetime, _dt.date)):
            job_dict[_k] = _v.strftime('%Y-%m-%d %H:%M:%S') if isinstance(_v, _dt.datetime) else _v.isoformat()

    # Get items (Format 1) — exclude soft-deleted, honor display_order
    cursor.execute(
        "SELECT * FROM items WHERE job_id = ? AND COALESCE(is_deleted, 0) = 0 "
        "ORDER BY COALESCE(display_order, 0) ASC, id ASC",
        (job_id,),
    )
    job_dict['items'] = [dict(row) for row in cursor.fetchall()]

    # Get declarations (Format 2)
    cursor.execute("SELECT * FROM declarations WHERE job_id = ?", (job_id,))
    job_dict['declarations'] = [dict(row) for row in cursor.fetchall()]

    # Get logs
    cursor.execute("SELECT * FROM processing_logs WHERE job_id = ?", (job_id,))
    job_dict['logs'] = [dict(row) for row in cursor.fetchall()]

    # Get metadata
    cursor.execute("SELECT metadata_json FROM pdf_metadata WHERE job_id = ?", (job_id,))
    metadata_row = cursor.fetchone()
    if metadata_row:
        job_dict['pdf_metadata'] = json.loads(metadata_row['metadata_json'])

    # Parse cross_validation JSON if present
    if job_dict.get('cross_validation_json'):
        try:
            job_dict['cross_validation'] = json.loads(job_dict['cross_validation_json'])
        except (json.JSONDecodeError, TypeError):
            job_dict['cross_validation'] = None
    else:
        job_dict['cross_validation'] = None

    # Parse field_bboxes JSON if present (V11 PDF↔form linking)
    if job_dict.get('field_bboxes_json'):
        try:
            job_dict['field_bboxes'] = json.loads(job_dict['field_bboxes_json'])
        except (json.JSONDecodeError, TypeError):
            job_dict['field_bboxes'] = {}
    else:
        job_dict['field_bboxes'] = {}

    conn.close()
    return job_dict


def update_job_field_bboxes(job_id: str, bboxes: Dict) -> bool:
    """Persist field bbox data on the job row (V11)."""
    if not job_id:
        return False
    try:
        payload = json.dumps(bboxes or {}, default=str)[:200000]
    except Exception:
        payload = "{}"
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE jobs SET field_bboxes_json = ? WHERE job_id = ?",
                    (payload, job_id))
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        print(f"[update_job_field_bboxes] {e}")
        return False
    finally:
        conn.close()

def get_stats() -> Dict:
    """Get database statistics"""

    conn = _connect()
    cursor = conn.cursor()

    # Total jobs
    cursor.execute("SELECT COUNT(*) FROM jobs")
    total_jobs = cursor.fetchone()[0]

    # Completed jobs
    cursor.execute("SELECT COUNT(*) FROM jobs WHERE status = 'COMPLETED'")
    completed_jobs = cursor.fetchone()[0]

    # Failed jobs
    cursor.execute("SELECT COUNT(*) FROM jobs WHERE status = 'FAILED'")
    failed_jobs = cursor.fetchone()[0]

    # Total items extracted (Format 1)
    cursor.execute("SELECT COUNT(*) FROM items")
    total_items = cursor.fetchone()[0]

    # Total declarations extracted (Format 2)
    cursor.execute("SELECT COUNT(*) FROM declarations")
    total_declarations = cursor.fetchone()[0]

    # Average accuracy
    cursor.execute("SELECT AVG(accuracy_percent) FROM jobs WHERE status = 'COMPLETED'")
    avg_accuracy = cursor.fetchone()[0] or 0.0

    # Total cost
    cursor.execute("SELECT SUM(cost_usd) FROM jobs WHERE status = 'COMPLETED'")
    total_cost = cursor.fetchone()[0] or 0.0

    conn.close()

    return {
        'total_jobs': total_jobs,
        'completed_jobs': completed_jobs,
        'failed_jobs': failed_jobs,
        'total_items': total_items,
        'total_declarations': total_declarations,
        'avg_accuracy': avg_accuracy,
        'total_cost': total_cost
    }

def find_job_by_hash(pdf_hash: str) -> Optional[Dict]:
    """Find a completed job with the same PDF hash (duplicate detection)."""
    if not pdf_hash:
        return None

    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM jobs
        WHERE pdf_hash = ? AND status = 'COMPLETED'
        ORDER BY created_at DESC
        LIMIT 1
    """, (pdf_hash,))

    row = cursor.fetchone()
    conn.close()

    if row:
        return dict(row)
    return None


def delete_job(job_id: str) -> bool:
    """Delete a job and all related data."""
    conn = _connect()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM processing_logs WHERE job_id = ?", (job_id,))
        cursor.execute("DELETE FROM pdf_metadata WHERE job_id = ?", (job_id,))
        cursor.execute("DELETE FROM items WHERE job_id = ?", (job_id,))
        cursor.execute("DELETE FROM declarations WHERE job_id = ?", (job_id,))
        cursor.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        conn.commit()
        conn.close()
        return True
    except Exception:
        conn.close()
        return False


# =============================================================================
# USER MANAGEMENT
# =============================================================================

def authenticate_user(username: str, password: str) -> Optional[Dict]:
    """Authenticate user with bcrypt. Falls back to SHA256 for migration."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,))
    user = cursor.fetchone()

    if not user:
        conn.close()
        return None

    stored_hash = user['password_hash']
    authenticated = False

    # Try bcrypt first (new format)
    if stored_hash.startswith('$2'):
        try:
            authenticated = bcrypt.checkpw(password.encode(), stored_hash.encode())
        except Exception:
            authenticated = False
    else:
        # Legacy SHA256 fallback — migrate to bcrypt on success
        sha_hash = hashlib.sha256(password.encode()).hexdigest()
        if sha_hash == stored_hash:
            authenticated = True
            new_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user['id']))
            logger.info(f"Migrated user {username} password to bcrypt")

    if authenticated:
        cursor.execute("UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?", (user['id'],))
        conn.commit()
        user_dict = dict(user)
        conn.close()
        return user_dict

    conn.close()
    return None


def create_user(username: str, password: str, display_name: str, role: str = 'user') -> bool:
    """Create a new user. Returns True on success."""
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    conn = _connect()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO users (username, password_hash, display_name, role)
            VALUES (?, ?, ?, ?)
        """, (username, password_hash, display_name, role))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False


def get_all_users() -> List[Dict]:
    """Get all users."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, display_name, role, is_active, created_at, last_login FROM users ORDER BY created_at")
    users = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return users


def update_user(user_id: int, display_name: str = None, role: str = None, is_active: int = None, password: str = None):
    """Update user fields."""
    conn = _connect()
    cursor = conn.cursor()

    if display_name is not None:
        cursor.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, user_id))
    if role is not None:
        cursor.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    if is_active is not None:
        cursor.execute("UPDATE users SET is_active = ? WHERE id = ?", (is_active, user_id))
    if password is not None:
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))

    conn.commit()
    conn.close()


def delete_user(user_id: int) -> bool:
    """Delete a user by ID."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


# =============================================================================
# PAGE CONTENTS — RAG STORAGE
# =============================================================================

def save_page_contents(job_id: str, pdf_name: str, pages: List[Dict], user_id: int = None):
    """Save page-by-page content to database and FTS index."""
    conn = _connect()
    cursor = conn.cursor()

    for page in pages:
        content = page.get('content', '')
        has_tables = 1 if any(kw in content.lower() for kw in ['|', 'total', 'qty', 'amount', 'rate', 'price']) else 0
        has_numbers = 1 if any(c.isdigit() for c in content) else 0

        cursor.execute("""
            INSERT INTO page_contents (job_id, user_id, pdf_name, page_number, page_type,
                source_agent, content, char_count, has_tables, has_numbers,
                ocr_status, skip, filter_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job_id, user_id, pdf_name,
            page.get('page', 0),
            page.get('type', ''),
            page.get('source', ''),
            content,
            len(content),
            has_tables, has_numbers,
            page.get('ocr_status', ''),
            1 if page.get('skip') else 0,
            page.get('filter_reason', '')
        ))

        # Postgres full-text: maintain tsvector column on the same row.
        row_id = cursor.lastrowid
        if row_id is not None:
            try:
                cursor.execute(
                    "UPDATE page_contents SET content_tsv = "
                    "to_tsvector('english', COALESCE(content, '') || ' ' || COALESCE(pdf_name, '')) "
                    "WHERE id = ?",
                    (row_id,),
                )
            except Exception:
                pass

    conn.commit()
    conn.close()
    print(f"  Saved {len(pages)} pages for job {job_id}")


def search_page_contents(query: str, user_id: int = None, pdf_name: str = None,
                         page_type: str = None, limit: int = 100) -> List[Dict]:
    """Full-text search across page contents."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    if query and query.strip():
        # Postgres full-text search via tsvector column. ts_headline emulates
        # SQLite FTS5 highlight() with **bold** delimiters.
        tsq_terms = ' | '.join(query.strip().split())
        sql = """
            SELECT pc.*,
                   ts_headline('english', COALESCE(pc.content, ''),
                               to_tsquery('english', ?),
                               'StartSel=**, StopSel=**, MaxFragments=1, MaxWords=20, MinWords=5'
                              ) AS snippet
            FROM page_contents pc
            WHERE pc.content_tsv @@ to_tsquery('english', ?)
        """
        params = [tsq_terms, tsq_terms]
    else:
        sql = "SELECT pc.*, '' as snippet FROM page_contents pc WHERE 1=1"
        params = []

    if user_id:
        sql += " AND pc.user_id = ?"
        params.append(user_id)
    if pdf_name and pdf_name != "All PDFs":
        sql += " AND pc.pdf_name = ?"
        params.append(pdf_name)
    if page_type and page_type != "All Types":
        sql += " AND pc.page_type = ?"
        params.append(page_type)

    sql += " ORDER BY pc.created_at DESC, pc.page_number ASC LIMIT ?"
    params.append(limit)

    try:
        cursor.execute(sql, params)
        results = [dict(row) for row in cursor.fetchall()]
    except Exception:
        results = []

    conn.close()
    return results


def get_all_page_contents(user_id: int = None, pdf_name: str = None,
                          page_type: str = None, limit: int = 500) -> List[Dict]:
    """Get all page contents with optional filters."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    sql = "SELECT * FROM page_contents WHERE skip = 0"
    params = []

    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    if pdf_name and pdf_name != "All PDFs":
        sql += " AND pdf_name = ?"
        params.append(pdf_name)
    if page_type and page_type != "All Types":
        sql += " AND page_type = ?"
        params.append(page_type)

    sql += " ORDER BY created_at DESC, page_number ASC LIMIT ?"
    params.append(limit)

    cursor.execute(sql, params)
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results


def get_page_content_pdfs(user_id: int = None) -> List[str]:
    """Get list of PDF names that have stored page content."""
    conn = _connect()
    cursor = conn.cursor()

    if user_id:
        cursor.execute("SELECT DISTINCT pdf_name FROM page_contents WHERE user_id = ? ORDER BY pdf_name", (user_id,))
    else:
        cursor.execute("SELECT DISTINCT pdf_name FROM page_contents ORDER BY pdf_name")

    pdfs = [row[0] for row in cursor.fetchall()]
    conn.close()
    return pdfs


def get_page_content_stats(user_id: int = None) -> Dict:
    """Get stats for stored page contents."""
    conn = _connect()
    cursor = conn.cursor()

    if user_id:
        cursor.execute("SELECT COUNT(*) FROM page_contents WHERE user_id = ?", (user_id,))
        total_pages = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(DISTINCT pdf_name) FROM page_contents WHERE user_id = ?", (user_id,))
        total_pdfs = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(char_count) FROM page_contents WHERE user_id = ?", (user_id,))
        total_chars = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM page_contents WHERE user_id = ? AND page_type = 'TEXT'", (user_id,))
        text_pages = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM page_contents WHERE user_id = ? AND page_type = 'IMAGE'", (user_id,))
        image_pages = cursor.fetchone()[0]
    else:
        cursor.execute("SELECT COUNT(*) FROM page_contents")
        total_pages = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(DISTINCT pdf_name) FROM page_contents")
        total_pdfs = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(char_count) FROM page_contents")
        total_chars = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM page_contents WHERE page_type = 'TEXT'")
        text_pages = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM page_contents WHERE page_type = 'IMAGE'")
        image_pages = cursor.fetchone()[0]

    conn.close()
    return {
        'total_pages': total_pages,
        'total_pdfs': total_pdfs,
        'total_chars': total_chars,
        'text_pages': text_pages,
        'image_pages': image_pages
    }


# =============================================================================
# ACTIVITY LOGGING
# =============================================================================

def log_activity(user_id: int, username: str, action: str, detail: str = ""):
    """Log a user activity."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO activity_logs (user_id, username, action, detail)
        VALUES (?, ?, ?, ?)
    """, (user_id, username, action, detail))
    conn.commit()
    conn.close()


def get_activity_logs(limit: int = 200, user_id: int = None) -> List[Dict]:
    """Get activity logs. Optionally filter by user."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    if user_id:
        cursor.execute("""
            SELECT * FROM activity_logs WHERE user_id = ?
            ORDER BY created_at DESC LIMIT ?
        """, (user_id, limit))
    else:
        cursor.execute("""
            SELECT * FROM activity_logs
            ORDER BY created_at DESC LIMIT ?
        """, (limit,))

    logs = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return logs


# =============================================================================
# PER-USER QUERIES
# =============================================================================

def get_user_jobs(user_id: int, limit: int = 100) -> List[Dict]:
    """Get jobs for a specific user."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM jobs WHERE user_id = ?
        ORDER BY created_at DESC LIMIT ?
    """, (user_id, limit))

    jobs = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jobs


def get_user_stats(user_id: int) -> Dict:
    """Get stats for a specific user."""
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (user_id,))
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'COMPLETED'", (user_id,))
    completed = cursor.fetchone()[0]

    cursor.execute("SELECT AVG(accuracy_percent) FROM jobs WHERE user_id = ? AND status = 'COMPLETED'", (user_id,))
    avg_acc = cursor.fetchone()[0] or 0.0

    cursor.execute("SELECT SUM(cost_usd) FROM jobs WHERE user_id = ? AND status = 'COMPLETED'", (user_id,))
    total_cost = cursor.fetchone()[0] or 0.0

    conn.close()
    return {'total_jobs': total, 'completed_jobs': completed, 'avg_accuracy': avg_acc, 'total_cost': total_cost}


# =============================================================================
# SETTINGS — KEY-VALUE STORE
# =============================================================================

def get_setting(key: str) -> Optional[str]:
    """Get a single setting value by key."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def set_setting(key: str, value: str, updated_by: str = "system"):
    """Set a single setting value (upsert)."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO settings (key, value, updated_at, updated_by)
        VALUES (?, ?, CURRENT_TIMESTAMP, ?)
        ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ?
    """, (key, value, updated_by, value, updated_by))
    conn.commit()
    conn.close()


def get_settings_by_prefix(prefix: str) -> Dict:
    """Get all settings matching a prefix as a dict."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT key, value, updated_at, updated_by FROM settings WHERE key LIKE ?", (f"{prefix}%",))
    rows = cursor.fetchall()
    conn.close()
    return {row["key"]: {"value": row["value"], "updated_at": row["updated_at"], "updated_by": row["updated_by"]} for row in rows}


def delete_settings_by_prefix(prefix: str):
    """Delete all settings matching a prefix."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM settings WHERE key LIKE ?", (f"{prefix}%",))
    conn.commit()
    conn.close()


# =============================================================================
# CORRECTIONS — Self-Learning
# =============================================================================

def save_correction(job_id: str, profile_id: int, table_key: str, field_key: str,
                    item_index: int, original_value: str, corrected_value: str,
                    correction_type: str = "wrong_value", user_id: int = None,
                    username: str = None) -> int:
    """Save a user correction. Returns correction id."""
    conn = _connect()
    conn.execute("PRAGMA foreign_keys = OFF")  # Corrections can reference any job_id
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO corrections (job_id, profile_id, table_key, field_key,
            item_index, original_value, corrected_value, correction_type,
            user_id, username)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (job_id, profile_id, table_key, field_key, item_index,
          str(original_value), str(corrected_value), correction_type,
          user_id, username))
    correction_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return correction_id


def get_corrections(profile_id: int = None, job_id: str = None,
                    table_key: str = None, field_key: str = None,
                    limit: int = 100) -> list:
    """Query corrections with optional filters."""
    conn = _connect()
    query = "SELECT * FROM corrections WHERE 1=1"
    params = []
    if profile_id:
        query += " AND profile_id = ?"
        params.append(profile_id)
    if job_id:
        query += " AND job_id = ?"
        params.append(job_id)
    if table_key:
        query += " AND table_key = ?"
        params.append(table_key)
    if field_key:
        query += " AND field_key = ?"
        params.append(field_key)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    columns = ["id", "job_id", "profile_id", "table_key", "field_key",
               "item_index", "original_value", "corrected_value",
               "correction_type", "page_source", "raw_text_context",
               "user_id", "username", "created_at"]
    return [dict(zip(columns, row)) for row in rows]


def get_correction_stats(profile_id: int = 1) -> list:
    """Get correction counts grouped by table_key + field_key."""
    conn = _connect()
    rows = conn.execute("""
        SELECT table_key, field_key, COUNT(*) as count,
               MIN(created_at) as first_at, MAX(created_at) as last_at
        FROM corrections
        WHERE profile_id = ?
        GROUP BY table_key, field_key
        ORDER BY count DESC
    """, (profile_id,)).fetchall()
    conn.close()
    return [{"table_key": r[0], "field_key": r[1], "count": r[2],
             "first_at": r[3], "last_at": r[4]} for r in rows]


def get_correction_count_for_field(profile_id: int, table_key: str, field_key: str) -> int:
    """Get number of corrections for a specific field."""
    conn = _connect()
    row = conn.execute("""
        SELECT COUNT(*) FROM corrections
        WHERE profile_id = ? AND table_key = ? AND field_key = ?
    """, (profile_id, table_key, field_key)).fetchone()
    conn.close()
    return row[0] if row else 0


def save_learning_event(profile_id: int, event_type: str, event_data: str,
                        trigger_correction_id: int = None,
                        corrections_analyzed: int = 0) -> int:
    """Record a learning event."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO learning_events (profile_id, event_type, event_data,
            trigger_correction_id, corrections_analyzed)
        VALUES (?, ?, ?, ?, ?)
    """, (profile_id, event_type, event_data, trigger_correction_id, corrections_analyzed))
    event_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return event_id


def get_learning_events(profile_id: int = 1, limit: int = 50) -> list:
    """Get learning events for a profile."""
    conn = _connect()
    rows = conn.execute("""
        SELECT id, profile_id, event_type, event_data,
               trigger_correction_id, corrections_analyzed, created_at
        FROM learning_events
        WHERE profile_id = ?
        ORDER BY created_at DESC LIMIT ?
    """, (profile_id, limit)).fetchall()
    conn.close()
    return [{"id": r[0], "profile_id": r[1], "event_type": r[2],
             "event_data": r[3], "trigger_correction_id": r[4],
             "corrections_analyzed": r[5], "created_at": r[6]} for r in rows]


# =============================================================================
# GROUPS — RBAC
# =============================================================================

ALL_PERMISSIONS = {
    "pages": {"agent": True, "history": True, "items": True, "declarations": True, "costs": True, "settings": True},
    "actions": {"run_pipeline": True, "upload_pdf": True, "download_excel": True, "delete_jobs": True, "export_data": True},
    "data_scope": "all_full",
}

DEFAULT_PERMISSIONS = {
    "pages": {"agent": True, "history": True, "items": True, "declarations": True, "costs": True, "settings": False},
    "actions": {"run_pipeline": True, "upload_pdf": True, "download_excel": True, "delete_jobs": False, "export_data": True},
    "data_scope": "own",
}


def create_group(name: str, description: str = "", **kwargs) -> Optional[int]:
    """Create a new group. Returns group id or None if name exists."""
    conn = _connect()
    cursor = conn.cursor()
    try:
        cols = ["name", "description"]
        vals = [name, description]
        for k in ["page_agent", "page_history", "page_items", "page_declarations",
                   "page_costs", "page_settings", "action_run_pipeline", "action_upload_pdf",
                   "action_download_excel", "action_delete_jobs", "action_export_data"]:
            if k in kwargs:
                cols.append(k)
                vals.append(1 if kwargs[k] else 0)
        if "data_scope" in kwargs:
            cols.append("data_scope")
            vals.append(kwargs["data_scope"])
        placeholders = ",".join(["?"] * len(vals))
        col_str = ",".join(cols)
        cursor.execute(f"INSERT INTO groups ({col_str}) VALUES ({placeholders})", vals)
        gid = cursor.lastrowid
        conn.commit()
        conn.close()
        return gid
    except sqlite3.IntegrityError:
        conn.close()
        return None


def update_group(group_id: int, **kwargs):
    """Update group fields."""
    conn = _connect()
    cursor = conn.cursor()
    sets = []
    vals = []
    for k, v in kwargs.items():
        if k in ["name", "description", "data_scope"]:
            sets.append(f"{k} = ?")
            vals.append(v)
        elif k.startswith("page_") or k.startswith("action_"):
            sets.append(f"{k} = ?")
            vals.append(1 if v else 0)
    if sets:
        vals.append(group_id)
        cursor.execute(f"UPDATE groups SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def delete_group(group_id: int) -> bool:
    """Delete a group and its member assignments."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_groups WHERE group_id = ?", (group_id,))
    cursor.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_all_groups() -> List[Dict]:
    """Get all groups with member count."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT g.*, COUNT(ug.user_id) as member_count
        FROM groups g
        LEFT JOIN user_groups ug ON g.id = ug.group_id
        GROUP BY g.id
        ORDER BY g.name
    """)
    groups = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return groups


def get_group(group_id: int) -> Optional[Dict]:
    """Get a single group by ID."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM groups WHERE id = ?", (group_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_group_members(group_id: int) -> List[Dict]:
    """Get all users in a group."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.id, u.username, u.display_name, u.email, u.role, u.keycloak_id
        FROM users u
        JOIN user_groups ug ON u.id = ug.user_id
        WHERE ug.group_id = ?
        ORDER BY u.username
    """, (group_id,))
    members = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return members


def set_user_group(user_id: int, group_id: Optional[int], assigned_by: str = "admin"):
    """Assign a user to a group. Pass group_id=None to remove from all groups."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_groups WHERE user_id = ?", (user_id,))
    if group_id is not None:
        cursor.execute(
            "INSERT INTO user_groups (user_id, group_id, assigned_by) VALUES (?, ?, ?)",
            (user_id, group_id, assigned_by),
        )
    conn.commit()
    conn.close()


def set_group_members(group_id: int, user_ids: List[int], assigned_by: str = "admin"):
    """Set the full member list for a group (replace existing)."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_groups WHERE group_id = ?", (group_id,))
    for uid in user_ids:
        cursor.execute(
            "INSERT OR IGNORE INTO user_groups (user_id, group_id, assigned_by) VALUES (?, ?, ?)",
            (uid, group_id, assigned_by),
        )
    conn.commit()
    conn.close()


def get_user_group(user_id: int) -> Optional[Dict]:
    """Get the group a user belongs to (first group if multiple)."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT g.* FROM groups g
        JOIN user_groups ug ON g.id = ug.group_id
        WHERE ug.user_id = ?
        LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_permissions(user: Dict) -> Dict:
    """Get full permission dict for a user. Admin gets all permissions."""
    if user.get("role") == "admin":
        return ALL_PERMISSIONS

    group = get_user_group(user["id"])
    if group:
        return {
            "pages": {
                "agent": bool(group["page_agent"]),
                "history": bool(group["page_history"]),
                "items": bool(group["page_items"]),
                "declarations": bool(group["page_declarations"]),
                "costs": bool(group["page_costs"]),
                "settings": bool(group["page_settings"]),
            },
            "actions": {
                "run_pipeline": bool(group["action_run_pipeline"]),
                "upload_pdf": bool(group["action_upload_pdf"]),
                "download_excel": bool(group["action_download_excel"]),
                "delete_jobs": bool(group["action_delete_jobs"]),
                "export_data": bool(group["action_export_data"]),
            },
            "data_scope": group.get("data_scope", "own"),
        }

    return DEFAULT_PERMISSIONS


def get_all_users_with_groups() -> List[Dict]:
    """Get all users with their group info."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.id, u.username, u.display_name, u.email, u.role, u.is_active,
               u.keycloak_id, u.created_at, u.last_login,
               g.id as group_id, g.name as group_name
        FROM users u
        LEFT JOIN user_groups ug ON u.id = ug.user_id
        LEFT JOIN groups g ON ug.group_id = g.id
        ORDER BY u.created_at
    """)
    users = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return users


# =============================================================================
# KEYCLOAK USER UPSERT
# =============================================================================

def upsert_keycloak_user(keycloak_id: str, username: str, display_name: str,
                         email: str, role: str) -> Dict:
    """
    Insert or update a Keycloak user. Adopts existing local user if username matches.
    Returns user dict with integer PK (preserves FK relationships).
    """
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 1. Try to find by keycloak_id (existing Keycloak user)
    cursor.execute("SELECT * FROM users WHERE keycloak_id = ?", (keycloak_id,))
    user = cursor.fetchone()
    if user:
        cursor.execute("""
            UPDATE users SET username = ?, display_name = ?, email = ?, role = ?,
                             last_login = CURRENT_TIMESTAMP
            WHERE keycloak_id = ?
        """, (username, display_name, email, role, keycloak_id))
        conn.commit()
        user_dict = dict(user)
        user_dict.update({"username": username, "display_name": display_name, "email": email, "role": role})
        conn.close()
        return user_dict

    # 2. Try to adopt existing local user by username match
    cursor.execute("SELECT * FROM users WHERE username = ? AND keycloak_id IS NULL", (username,))
    user = cursor.fetchone()
    if user:
        cursor.execute("""
            UPDATE users SET keycloak_id = ?, display_name = ?, email = ?, role = ?,
                             last_login = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (keycloak_id, display_name, email, role, user["id"]))
        conn.commit()
        user_dict = dict(user)
        user_dict.update({"keycloak_id": keycloak_id, "display_name": display_name, "email": email, "role": role})
        conn.close()
        return user_dict

    # 3. Create new user
    cursor.execute("""
        INSERT INTO users (username, password_hash, display_name, role, keycloak_id, email, last_login)
        VALUES (?, '', ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (username, display_name, role, keycloak_id, email))
    new_id = cursor.lastrowid
    conn.commit()

    cursor.execute("SELECT * FROM users WHERE id = ?", (new_id,))
    user = cursor.fetchone()
    user_dict = dict(user) if user else {"id": new_id, "username": username, "role": role, "display_name": display_name}
    conn.close()
    return user_dict


# =============================================================================
# PAGE EXTRACTIONS — v2 per-page structured data
# =============================================================================

def save_page_extractions(job_id: str, page_results: List[Dict]):
    """Save v2 per-page extraction results."""
    conn = _connect()
    cursor = conn.cursor()

    for pr in page_results:
        parsed = pr.get("parsed", {})
        doc = parsed.get("document", {})
        visual = parsed.get("visual", {})
        entities = parsed.get("entities", {})

        cursor.execute("""
            INSERT INTO page_extractions
                (job_id, page_number, page_type, language, confidence, explanation,
                 doc_title, doc_issuer, doc_date, doc_reference, doc_country,
                 fields_json, items_json, amounts_json, entities_json,
                 has_logo, has_stamp, has_signature, has_barcode, visual_quality,
                 raw_char_count, orientation, pipeline_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v2')
        """, (
            job_id,
            pr.get("page_number", 0),
            pr.get("page_type", "unknown"),
            parsed.get("language", ""),
            pr.get("confidence", 0),
            pr.get("explanation", ""),
            doc.get("title", ""),
            doc.get("issuer", ""),
            doc.get("date", ""),
            doc.get("reference", ""),
            doc.get("country", ""),
            json.dumps(parsed.get("fields", {}), ensure_ascii=False, default=str),
            json.dumps(parsed.get("items", []), ensure_ascii=False, default=str),
            json.dumps(parsed.get("amounts", []), ensure_ascii=False, default=str),
            json.dumps(entities, ensure_ascii=False, default=str),
            1 if visual.get("has_logo") else 0,
            1 if visual.get("has_stamp") else 0,
            1 if visual.get("has_signature") else 0,
            1 if visual.get("has_barcode") else 0,
            visual.get("quality", ""),
            pr.get("raw_char_count", 0),
            pr.get("orientation", "portrait"),
        ))

    conn.commit()
    conn.close()
    print(f"  Saved {len(page_results)} page extractions for {job_id}")


def get_page_extractions(job_id: str) -> List[Dict]:
    """Get all page extractions for a job."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM page_extractions WHERE job_id = ? ORDER BY page_number
    """, (job_id,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    for row in rows:
        for jf in ('fields_json', 'items_json', 'amounts_json', 'entities_json'):
            if row.get(jf):
                try:
                    row[jf.replace('_json', '')] = json.loads(row.pop(jf))
                except (json.JSONDecodeError, TypeError, ValueError):
                    row[jf.replace('_json', '')] = {}
                    del row[jf]
            else:
                row[jf.replace('_json', '')] = {} if 'fields' in jf or 'entities' in jf else []
                if jf in row:
                    del row[jf]
    return rows


def _normalize_importer(name: str) -> str:
    """Normalize importer name for matching (strip suffixes, uppercase)."""
    import re
    n = str(name).upper().strip()
    n = re.sub(r'\s*(CO\.,?\s*LTD\.?|COMPANY\s+LIMITED|PTE\s+LTD\.?|LTD\.?)\s*$', '', n).strip()
    n = re.sub(r'\s+', ' ', n)
    return n


def get_importer_profile(importer_name: str) -> Optional[Dict]:
    """Get learned profile for an importer."""
    norm = _normalize_importer(importer_name)
    if not norm:
        return None
    conn = _connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM importer_profiles WHERE importer_name_normalized = ?", (norm,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_importer_profile(importer_name: str, currency: str = None,
                            exchange_rate: float = None, consignor: str = None,
                            items_summary: str = None):
    """Update or create importer profile from completed job data."""
    norm = _normalize_importer(importer_name)
    if not norm:
        return
    conn = _connect()
    existing = conn.execute(
        "SELECT * FROM importer_profiles WHERE importer_name_normalized = ?", (norm,)
    ).fetchone()

    if existing:
        # Update running stats
        total_jobs = (existing[10] or 0) + 1  # total_jobs column
        old_min = existing[5] or exchange_rate or 0
        old_max = existing[6] or exchange_rate or 0
        old_avg = existing[7] or exchange_rate or 0
        new_min = min(old_min, exchange_rate) if exchange_rate else old_min
        new_max = max(old_max, exchange_rate) if exchange_rate else old_max
        new_avg = ((old_avg * (total_jobs - 1)) + (exchange_rate or old_avg)) / total_jobs

        conn.execute("""
            UPDATE importer_profiles SET
                currency = COALESCE(?, currency),
                exchange_rate_min = ?, exchange_rate_max = ?, exchange_rate_avg = ?,
                common_consignor = COALESCE(?, common_consignor),
                common_items = COALESCE(?, common_items),
                total_jobs = ?,
                last_job_date = datetime('now'),
                updated_at = datetime('now')
            WHERE importer_name_normalized = ?
        """, (currency, new_min, new_max, round(new_avg, 4),
              consignor, items_summary, total_jobs, norm))
    else:
        conn.execute("""
            INSERT INTO importer_profiles
                (importer_name, importer_name_normalized, currency,
                 exchange_rate_min, exchange_rate_max, exchange_rate_avg,
                 common_consignor, common_items, total_jobs, last_job_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
        """, (importer_name, norm, currency,
              exchange_rate, exchange_rate, exchange_rate,
              consignor, items_summary))

    conn.commit()
    conn.close()


def update_field_accuracy(importer_name: str, field_key: str, was_corrected: bool = False):
    """Track field accuracy per importer."""
    norm = _normalize_importer(importer_name)
    if not norm:
        return
    conn = _connect()
    conn.execute("""
        INSERT INTO field_accuracy (importer_name_normalized, field_key, total_extractions, corrections_count)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(importer_name_normalized, field_key)
        DO UPDATE SET
            total_extractions = total_extractions + 1,
            corrections_count = corrections_count + ?,
            last_correction_at = CASE WHEN ? THEN datetime('now') ELSE last_correction_at END
    """, (norm, field_key, 1 if was_corrected else 0,
          1 if was_corrected else 0, was_corrected))
    conn.commit()
    conn.close()


def get_weak_fields(importer_name: str, min_error_rate: float = 0.3) -> List[str]:
    """Get fields that have high error rate for an importer."""
    norm = _normalize_importer(importer_name)
    if not norm:
        return []
    conn = _connect()
    rows = conn.execute("""
        SELECT field_key, total_extractions, corrections_count
        FROM field_accuracy
        WHERE importer_name_normalized = ? AND total_extractions >= 2
    """, (norm,)).fetchall()
    conn.close()
    weak = []
    for field, total, corrections in rows:
        if total > 0 and corrections / total >= min_error_rate:
            weak.append(field)
    return weak


def get_recent_declarations_by_importer(importer_name: str, limit: int = 5) -> list:
    """Recent declarations for same importer — for baseline cross-check."""
    if not importer_name:
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT currency, exchange_rate, declaration_no, declaration_date
               FROM declarations
               WHERE importer_name = ?
               ORDER BY id DESC LIMIT ?""",
            (importer_name, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def get_fee_baseline(importer_name: str) -> dict:
    """Get verified fee baseline for an importer.
    Returns dict like {"SF": 20000, "MF": 30000, "CT_zero_ok": True} or empty dict.
    """
    norm = _normalize_importer(importer_name)
    if not norm:
        return {}
    conn = _connect()
    row = conn.execute(
        "SELECT fee_baseline_json FROM importer_profiles WHERE importer_name_normalized = ?",
        (norm,)
    ).fetchone()
    conn.close()
    if row and row[0]:
        try:
            import json
            return json.loads(row[0])
        except Exception:
            pass
    return {}


def save_fee_baseline(importer_name: str, fee_baseline: dict):
    """Save verified fee baseline for an importer.
    Called after user corrections or after successful extraction with no corrections.
    fee_baseline: {"SF": 20000, "MF": 30000, "CT": 0, "AT": 2608987, ...}
    """
    norm = _normalize_importer(importer_name)
    if not norm or not fee_baseline:
        return
    import json
    baseline_json = json.dumps(fee_baseline)
    conn = _connect()
    conn.execute("""
        UPDATE importer_profiles SET fee_baseline_json = ?, updated_at = datetime('now')
        WHERE importer_name_normalized = ?
    """, (baseline_json, norm))
    conn.commit()
    conn.close()


def save_value_audit(job_id: str, table_key: str, field_key: str,
                     stage: str, old_value: str, new_value: str,
                     source: str = "", item_index: int = None):
    """Record a value change in the audit trail."""
    conn = _connect()
    conn.execute("""
        INSERT INTO value_audit (job_id, table_key, field_key, item_index, stage, old_value, new_value, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (job_id, table_key, field_key, item_index, stage, str(old_value), str(new_value), source))
    conn.commit()
    conn.close()


# ─── Multi-LDAP CRUD helpers ─────────────────────────────
def list_ldap_configs(only_active: bool = False) -> list:
    conn = _connect(); cur = conn.cursor()
    sql = "SELECT id, label, host, port, use_tls, validate_cert, bind_dn, search_base, search_filter, attr_username, attr_mail, attr_groups, email_domain_hint, priority, active, created_at FROM ldap_configs"
    if only_active:
        sql += " WHERE active = 1"
    sql += " ORDER BY priority ASC, id ASC"
    cur.execute(sql)
    rows = cur.fetchall()
    conn.close()
    cols = ["id","label","host","port","use_tls","validate_cert","bind_dn","search_base","search_filter","attr_username","attr_mail","attr_groups","email_domain_hint","priority","active","created_at"]
    return [dict(zip(cols, r)) for r in rows]


def get_ldap_config(ldap_id: int, include_password: bool = False) -> dict:
    conn = _connect(); cur = conn.cursor()
    extra = ", bind_password_encrypted" if include_password else ""
    cur.execute(f"SELECT id, label, host, port, use_tls, validate_cert, bind_dn, search_base, search_filter, attr_username, attr_mail, attr_groups, email_domain_hint, priority, active{extra} FROM ldap_configs WHERE id = ?", (ldap_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    cols = ["id","label","host","port","use_tls","validate_cert","bind_dn","search_base","search_filter","attr_username","attr_mail","attr_groups","email_domain_hint","priority","active"]
    if include_password:
        cols.append("bind_password_encrypted")
    return dict(zip(cols, row))


def create_ldap_config(**kwargs) -> int:
    """kwargs: label, host, port, use_tls, validate_cert, bind_dn, bind_password_encrypted, search_base, search_filter, attr_username, attr_mail, attr_groups, email_domain_hint, priority, active"""
    conn = _connect(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO ldap_configs (label, host, port, use_tls, validate_cert, bind_dn, bind_password_encrypted, search_base, search_filter, attr_username, attr_mail, attr_groups, email_domain_hint, priority, active)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        kwargs.get("label"), kwargs.get("host"), kwargs.get("port", 389),
        1 if kwargs.get("use_tls") else 0,
        1 if kwargs.get("validate_cert") else 0,
        kwargs.get("bind_dn"), kwargs.get("bind_password_encrypted"),
        kwargs.get("search_base"), kwargs.get("search_filter"),
        kwargs.get("attr_username", "uid"), kwargs.get("attr_mail", "mail"),
        kwargs.get("attr_groups", "memberOf"), kwargs.get("email_domain_hint"),
        kwargs.get("priority", 50),
        1 if kwargs.get("active", True) else 0,
    ))
    new_id = cur.lastrowid
    conn.commit(); conn.close()
    return new_id


def update_ldap_config(ldap_id: int, **kwargs) -> bool:
    """Update only provided fields. Pass bind_password_encrypted (already encrypted) to change password."""
    if not kwargs:
        return False
    cols = []; vals = []
    for k in ("label","host","port","use_tls","validate_cert","bind_dn","bind_password_encrypted","search_base","search_filter","attr_username","attr_mail","attr_groups","email_domain_hint","priority","active"):
        if k in kwargs:
            cols.append(f"{k} = ?")
            v = kwargs[k]
            if k in ("use_tls","validate_cert","active"):
                v = 1 if v else 0
            vals.append(v)
    if not cols:
        return False
    cols.append("updated_at = CURRENT_TIMESTAMP")
    vals.append(ldap_id)
    conn = _connect(); cur = conn.cursor()
    cur.execute(f"UPDATE ldap_configs SET {', '.join(cols)} WHERE id = ?", vals)
    conn.commit(); n = cur.rowcount; conn.close()
    return n > 0


def delete_ldap_config(ldap_id: int) -> bool:
    conn = _connect(); cur = conn.cursor()
    cur.execute("DELETE FROM ldap_configs WHERE id = ?", (ldap_id,))
    conn.commit(); n = cur.rowcount; conn.close()
    return n > 0


def find_ldap_by_email_domain(domain: str) -> int:
    if not domain:
        return None
    conn = _connect(); cur = conn.cursor()
    cur.execute("SELECT id FROM ldap_configs WHERE active = 1 AND email_domain_hint = ? ORDER BY priority ASC LIMIT 1", (domain.lower(),))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def update_user_ldap_default(username: str, ldap_id: int, ldap_dn: str = None) -> bool:
    conn = _connect(); cur = conn.cursor()
    cur.execute("UPDATE users SET default_ldap_id = ?, ldap_dn = ? WHERE username = ?", (ldap_id, ldap_dn, username))
    conn.commit(); n = cur.rowcount; conn.close()
    return n > 0


# =============================================================================
# Storage config CRUD (S3 / GCS / Azure / local)
# =============================================================================

def list_storage_configs() -> list:
    conn = _connect(); conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""SELECT id, label, provider, endpoint_url, region_name, bucket_name,
                          access_key_id, key_prefix, use_ssl, addressing_style, signature_version,
                          use_for_uploads, use_for_exports, use_for_cache, use_for_archive,
                          active, created_at, updated_at
                   FROM storage_config ORDER BY id""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_storage_config(cfg_id: int, include_secret: bool = False) -> dict:
    conn = _connect(); conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    extra = ", secret_access_key_encrypted" if include_secret else ""
    cur.execute(f"""SELECT id, label, provider, endpoint_url, region_name, bucket_name,
                           access_key_id, key_prefix, use_ssl, addressing_style, signature_version,
                           use_for_uploads, use_for_exports, use_for_cache, use_for_archive,
                           active{extra}
                    FROM storage_config WHERE id = ?""", (cfg_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_active_storage_config(include_secret: bool = True) -> dict:
    """Return the active storage config (only one row should be active)."""
    conn = _connect(); conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""SELECT id, label, provider, endpoint_url, region_name, bucket_name,
                          access_key_id, secret_access_key_encrypted, key_prefix, use_ssl,
                          addressing_style, signature_version,
                          use_for_uploads, use_for_exports, use_for_cache, use_for_archive
                   FROM storage_config WHERE active = 1 LIMIT 1""")
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def create_storage_config(**kwargs) -> int:
    conn = _connect(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO storage_config (label, provider, endpoint_url, region_name, bucket_name,
            access_key_id, secret_access_key_encrypted, key_prefix, use_ssl,
            addressing_style, signature_version,
            use_for_uploads, use_for_exports, use_for_cache, use_for_archive, active)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        kwargs.get("label"), kwargs.get("provider", "local"),
        kwargs.get("endpoint_url"), kwargs.get("region_name"),
        kwargs.get("bucket_name"), kwargs.get("access_key_id"),
        kwargs.get("secret_access_key_encrypted"),
        kwargs.get("key_prefix", ""),
        1 if kwargs.get("use_ssl", True) else 0,
        kwargs.get("addressing_style", "auto"),
        kwargs.get("signature_version", "s3v4"),
        1 if kwargs.get("use_for_uploads", True) else 0,
        1 if kwargs.get("use_for_exports", True) else 0,
        1 if kwargs.get("use_for_cache", False) else 0,
        1 if kwargs.get("use_for_archive", False) else 0,
        1 if kwargs.get("active", False) else 0,
    ))
    new_id = cur.lastrowid
    conn.commit(); conn.close()
    return new_id


def update_storage_config(cfg_id: int, **kwargs) -> bool:
    if not kwargs: return False
    cols = []; vals = []
    bool_fields = ("use_ssl", "use_for_uploads", "use_for_exports", "use_for_cache",
                   "use_for_archive", "active")
    allowed = ("label", "provider", "endpoint_url", "region_name", "bucket_name",
               "access_key_id", "secret_access_key_encrypted", "key_prefix",
               "addressing_style", "signature_version") + bool_fields
    for k in allowed:
        if k in kwargs:
            cols.append(f"{k} = ?")
            v = kwargs[k]
            if k in bool_fields: v = 1 if v else 0
            vals.append(v)
    if not cols: return False
    cols.append("updated_at = CURRENT_TIMESTAMP")
    vals.append(cfg_id)
    conn = _connect(); cur = conn.cursor()
    cur.execute(f"UPDATE storage_config SET {', '.join(cols)} WHERE id = ?", vals)
    conn.commit(); n = cur.rowcount; conn.close()
    return n > 0


def delete_storage_config(cfg_id: int) -> bool:
    conn = _connect(); cur = conn.cursor()
    cur.execute("DELETE FROM storage_config WHERE id = ?", (cfg_id,))
    conn.commit(); n = cur.rowcount; conn.close()
    return n > 0


def activate_storage_config(cfg_id: int) -> bool:
    """Atomically set this config active and deactivate all others."""
    conn = _connect(); cur = conn.cursor()
    cur.execute("UPDATE storage_config SET active = 0")
    cur.execute("UPDATE storage_config SET active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (cfg_id,))
    n = cur.rowcount
    conn.commit(); conn.close()
    return n > 0


# ─── Review/Approve workflow helpers ────────────────────────────

# Field name → declarations column map (used by review PATCH endpoints)
DECLARATION_FIELD_MAP = {
    "declaration_no": "declaration_no",
    "declaration_date": "declaration_date",
    "importer_name": "importer_name",
    "consignor_name": "consignor_name",
    "invoice_number": "invoice_number",
    "invoice_number_customs_declaration": "invoice_number_customs_declaration",
    "invoice_number_commercial_invoice": "invoice_number_commercial_invoice",
    "invoice_price": "invoice_price",
    "currency": "currency",
    "exchange_rate": "exchange_rate",
    "currency_2": "currency_2",
    "total_customs_value": "total_customs_value",
    "import_export_customs_duty": "import_export_customs_duty",
    "commercial_tax_ct": "commercial_tax_ct",
    "advance_income_tax_at": "advance_income_tax_at",
    "security_fee_sf": "security_fee_sf",
    "maccs_service_fee_mf": "maccs_service_fee_mf",
    "exemption_reduction": "exemption_reduction",
    # Legacy display labels (frontend may send these)
    "Declaration No": "declaration_no",
    "Declaration Date": "declaration_date",
    "Importer (Name)": "importer_name",
    "Consignor (Name)": "consignor_name",
    "Invoice Number": "invoice_number",
    "Invoice Price": "invoice_price",
    "Currency": "currency",
    "Exchange Rate": "exchange_rate",
    "Total Customs Value": "total_customs_value",
    "Import/Export Customs Duty": "import_export_customs_duty",
    "Commercial Tax (CT)": "commercial_tax_ct",
    "Advance Income Tax (AT)": "advance_income_tax_at",
    "Security Fee (SF)": "security_fee_sf",
    "MACCS Service Fee (MF)": "maccs_service_fee_mf",
    "Exemption/Reduction": "exemption_reduction",
}

ITEM_FIELD_MAP = {
    "item_name": "item_name",
    "customs_duty_rate": "customs_duty_rate",
    "quantity": "quantity",
    "invoice_unit_price": "invoice_unit_price",
    "cif_unit_price": "cif_unit_price",
    "commercial_tax_percent": "commercial_tax_percent",
    "exchange_rate": "exchange_rate",
    "hs_code": "hs_code",
    "origin_country": "origin_country",
    "customs_value_mmk": "customs_value_mmk",
    # Legacy display labels
    "Item name": "item_name",
    "Customs duty rate": "customs_duty_rate",
    "Quantity (1)": "quantity",
    "Invoice unit price": "invoice_unit_price",
    "CIF unit price": "cif_unit_price",
    "Commercial tax %": "commercial_tax_percent",
    "Exchange Rate (1)": "exchange_rate",
    "HS Code": "hs_code",
    "Origin Country": "origin_country",
    "Customs Value (MMK)": "customs_value_mmk",
}

FEE_FIELD_KEYS = {
    "commercial_tax_ct", "advance_income_tax_at", "security_fee_sf",
    "maccs_service_fee_mf", "exemption_reduction",
}


def update_review_status(job_id: str, status: str,
                         reviewed_by: str = None, notes: str = None) -> bool:
    """Update jobs.review_status. Sets reviewed_by/reviewed_at when status is approved/rejected."""
    if status not in ("pending_review", "approved", "rejected", "draft"):
        raise ValueError(f"invalid review_status: {status}")
    conn = _connect()
    cur = conn.cursor()
    if status in ("approved", "rejected"):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            UPDATE jobs SET review_status = ?, reviewed_by = ?, reviewed_at = ?,
                review_notes = COALESCE(?, review_notes)
            WHERE job_id = ?
        """, (status, reviewed_by, ts, notes, job_id))
    else:
        cur.execute("""
            UPDATE jobs SET review_status = ?,
                review_notes = COALESCE(?, review_notes)
            WHERE job_id = ?
        """, (status, notes, job_id))
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n > 0


def log_field_edit(job_id: str, entity_type: str, entity_index: int,
                   field_name: str, original, corrected,
                   edited_by: str = None, page_ref: int = None,
                   reason: str = None) -> int:
    """Insert a row into field_edits audit trail. Returns new row id."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO field_edits
            (job_id, entity_type, entity_index, field_name,
             original_value, corrected_value, edited_by, pdf_page_ref, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (job_id, entity_type, entity_index or 0, field_name,
          None if original is None else str(original),
          None if corrected is None else str(corrected),
          edited_by, page_ref, reason))
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id


def list_field_edits(job_id: str) -> List[Dict]:
    """Return field_edits rows for a job, oldest first."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM field_edits WHERE job_id = ?
        ORDER BY id ASC
    """, (job_id,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_declaration_field(job_id: str, field_name: str):
    """Read current declaration field value for a job. Returns None if not found."""
    col = DECLARATION_FIELD_MAP.get(field_name)
    if not col:
        return None
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(f"SELECT {col} AS v FROM declarations WHERE job_id = ? ORDER BY id LIMIT 1",
                (job_id,))
    row = cur.fetchone()
    conn.close()
    return row["v"] if row else None


def update_declaration_field(job_id: str, field_name: str, value) -> bool:
    """Update a single declaration field for a job (first declaration row)."""
    col = DECLARATION_FIELD_MAP.get(field_name)
    if not col:
        return False
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"""
        UPDATE declarations SET {col} = ?
        WHERE id = (SELECT id FROM declarations WHERE job_id = ? ORDER BY id LIMIT 1)
    """, (value, job_id))
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n > 0


def get_item_field(job_id: str, item_index: int, field_name: str):
    """Read current item field value at item_index (0-based)."""
    col = ITEM_FIELD_MAP.get(field_name)
    if not col:
        return None
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM items WHERE job_id = ? AND COALESCE(is_deleted, 0) = 0 "
        "ORDER BY COALESCE(display_order, 0) ASC, id ASC",
        (job_id,),
    )
    rows = cur.fetchall()
    if item_index >= len(rows):
        conn.close()
        return None
    item_id = rows[item_index]["id"]
    cur.execute(f"SELECT {col} AS v FROM items WHERE id = ?", (item_id,))
    row = cur.fetchone()
    conn.close()
    return row["v"] if row else None


def update_item_field(job_id: str, item_index: int, field_name: str, value) -> bool:
    """Update a single item field by 0-based index for a job."""
    col = ITEM_FIELD_MAP.get(field_name)
    if not col:
        return False
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM items WHERE job_id = ? AND COALESCE(is_deleted, 0) = 0 "
        "ORDER BY COALESCE(display_order, 0) ASC, id ASC",
        (job_id,),
    )
    rows = cur.fetchall()
    if item_index >= len(rows):
        conn.close()
        return False
    item_id = rows[item_index][0]
    cur.execute(f"UPDATE items SET {col} = ? WHERE id = ?", (value, item_id))
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n > 0


def increment_edits_count(job_id: str) -> None:
    """Bump jobs.edits_count by 1."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        UPDATE jobs SET edits_count = COALESCE(edits_count, 0) + 1
        WHERE job_id = ?
    """, (job_id,))
    conn.commit()
    conn.close()


def list_review_queue(status: str = "pending_review", limit: int = 200,
                      importer: Optional[str] = None,
                      date_from: Optional[str] = None,
                      date_to: Optional[str] = None,
                      min_edits: Optional[int] = None,
                      max_edits: Optional[int] = None) -> List[Dict]:
    """List jobs in given review_status (default pending_review), most recent first.

    Optional filters:
      - importer: substring match (case-insensitive) on declarations.importer_name
      - date_from / date_to: ISO YYYY-MM-DD bounds on jobs.created_at
      - min_edits / max_edits: bounds on jobs.edits_count
    """
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    sql = [
        "SELECT j.job_id, j.pdf_name, j.status, j.review_status, j.reviewed_by, j.reviewed_at,",
        "       j.review_notes, j.edits_count, j.created_at, j.completed_at, j.username,",
        "       j.pipeline_mode, j.document_type, j.accuracy_percent,",
        "       (SELECT importer_name FROM declarations WHERE job_id = j.job_id ORDER BY id LIMIT 1) AS importer_name,",
        "       (SELECT COUNT(*) FROM items WHERE job_id = j.job_id AND COALESCE(is_deleted, 0) = 0) AS items_count",
        "FROM jobs j",
        "WHERE j.review_status = ?",
    ]
    params: List = [status]

    if importer:
        sql.append(
            "AND LOWER(COALESCE("
            "(SELECT importer_name FROM declarations WHERE job_id = j.job_id ORDER BY id LIMIT 1)"
            ", '')) LIKE ?"
        )
        params.append(f"%{importer.lower()}%")
    if date_from:
        sql.append("AND DATE(j.created_at) >= DATE(?)")
        params.append(date_from)
    if date_to:
        sql.append("AND DATE(j.created_at) <= DATE(?)")
        params.append(date_to)
    if min_edits is not None:
        sql.append("AND COALESCE(j.edits_count, 0) >= ?")
        params.append(int(min_edits))
    if max_edits is not None:
        sql.append("AND COALESCE(j.edits_count, 0) <= ?")
        params.append(int(max_edits))

    sql.append("ORDER BY j.created_at DESC LIMIT ?")
    params.append(limit)

    cur.execute(" ".join(sql), tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_review_stats() -> Dict:
    """Return counts by review_status plus auto_approved_today.

    Keys: pending_review, approved, rejected, draft, auto_approved_today, total.
    """
    conn = _connect()
    cur = conn.cursor()
    out = {"pending_review": 0, "approved": 0, "rejected": 0, "draft": 0}
    cur.execute("SELECT review_status, COUNT(*) FROM jobs GROUP BY review_status")
    for status, count in cur.fetchall():
        if status in out:
            out[status] = int(count or 0)
    cur.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE review_status = 'approved' AND reviewed_by = 'SYSTEM_AUTO' "
        "AND DATE(reviewed_at) = DATE('now')"
    )
    row = cur.fetchone()
    out["auto_approved_today"] = int(row[0] or 0) if row else 0
    cur.execute("SELECT COUNT(*) FROM jobs")
    row = cur.fetchone()
    out["total"] = int(row[0] or 0) if row else 0
    conn.close()
    return out


# =============================================================================
# APP SETTINGS — typed wrapper around the settings key-value store
# =============================================================================

def get_app_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read a single app setting value, returning default if missing."""
    val = get_setting(key)
    return val if val is not None else default


def set_app_setting(key: str, value, updated_by: str = "system") -> None:
    """Write a single app setting value (coerced to string)."""
    if isinstance(value, bool):
        s = "true" if value else "false"
    else:
        s = str(value)
    set_setting(key, s, updated_by=updated_by)


# ─── Items add / delete / reorder (V11 review UI) ──────────────────

# Map of accepted keys (snake_case + display labels) → DB columns for INSERT.
_ITEM_INSERT_COLS = {
    "item_name": "item_name",
    "customs_duty_rate": "customs_duty_rate",
    "quantity": "quantity",
    "invoice_unit_price": "invoice_unit_price",
    "cif_unit_price": "cif_unit_price",
    "commercial_tax_percent": "commercial_tax_percent",
    "exchange_rate": "exchange_rate",
    "hs_code": "hs_code",
    "origin_country": "origin_country",
    "customs_value_mmk": "customs_value_mmk",
    # legacy display labels
    "Item name": "item_name",
    "Customs duty rate": "customs_duty_rate",
    "Quantity (1)": "quantity",
    "Invoice unit price": "invoice_unit_price",
    "CIF unit price": "cif_unit_price",
    "Commercial tax %": "commercial_tax_percent",
    "Exchange Rate (1)": "exchange_rate",
    "HS Code": "hs_code",
    "Origin Country": "origin_country",
    "Customs Value (MMK)": "customs_value_mmk",
}


def add_item(job_id: str, item_dict: Dict) -> int:
    """Insert a new item row for a job. Returns the new item's id (DB pk).
    The new row is appended at the end (display_order = current max + 1)."""
    conn = _connect()
    cur = conn.cursor()

    # Normalize incoming keys → DB columns
    cols = {}
    for k, v in (item_dict or {}).items():
        target = _ITEM_INSERT_COLS.get(k)
        if target:
            cols[target] = v

    # Append at the end of current ordering
    cur.execute(
        "SELECT COALESCE(MAX(display_order), -1) FROM items "
        "WHERE job_id = ? AND COALESCE(is_deleted, 0) = 0",
        (job_id,),
    )
    next_order = (cur.fetchone()[0] or -1) + 1

    cols.setdefault("item_name", "")
    cols.setdefault("hs_code", "")
    cols.setdefault("quantity", "")
    cols["display_order"] = next_order

    keys = ["job_id"] + list(cols.keys())
    placeholders = ",".join(["?"] * len(keys))
    values = [job_id] + list(cols.values())

    cur.execute(
        f"INSERT INTO items ({','.join(keys)}) VALUES ({placeholders})",
        values,
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id


def delete_item(job_id: str, item_index: int) -> Optional[Dict]:
    """Soft-delete the item at item_index (0-based, in display order).
    Returns the deleted row dict (so caller can log original_value) or None."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM items WHERE job_id = ? AND COALESCE(is_deleted, 0) = 0 "
        "ORDER BY COALESCE(display_order, 0) ASC, id ASC",
        (job_id,),
    )
    rows = cur.fetchall()
    if item_index < 0 or item_index >= len(rows):
        conn.close()
        return None
    row = dict(rows[item_index])
    item_id = row["id"]
    cur.execute("UPDATE items SET is_deleted = 1 WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return row


def reorder_items(job_id: str, order_list: List[int]) -> List[Dict]:
    """Apply a new ordering. order_list is a permutation of current indexes;
    e.g. [2,0,1] means: item that is currently at index 2 moves to position 0, etc.
    Returns the items in new order."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM items WHERE job_id = ? AND COALESCE(is_deleted, 0) = 0 "
        "ORDER BY COALESCE(display_order, 0) ASC, id ASC",
        (job_id,),
    )
    rows = cur.fetchall()
    n = len(rows)
    if not order_list or len(order_list) != n:
        conn.close()
        raise ValueError(f"order length {len(order_list) if order_list else 0} != item count {n}")
    if sorted(order_list) != list(range(n)):
        conn.close()
        raise ValueError("order must be a permutation of indexes 0..n-1")

    # For each new position N, the row at old_index = order_list[N] gets display_order = N
    for new_pos, old_idx in enumerate(order_list):
        item_id = rows[old_idx]["id"]
        cur.execute(
            "UPDATE items SET display_order = ? WHERE id = ?",
            (new_pos, item_id),
        )
    conn.commit()
    conn.close()
    return get_job_items(job_id)


if __name__ == "__main__":
    # Initialize database
    init_database()

    # Show stats
    stats = get_stats()
    print("\n📊 Database Statistics:")
    print(f"Total Jobs: {stats['total_jobs']}")
    print(f"Completed: {stats['completed_jobs']}")
    print(f"Failed: {stats['failed_jobs']}")
    print(f"Total Items (Format 1): {stats['total_items']}")
    print(f"Total Declarations (Format 2): {stats['total_declarations']}")
    print(f"Avg Accuracy: {stats['avg_accuracy']:.1f}%")
    print(f"Total Cost: ${stats['total_cost']:.4f}")
