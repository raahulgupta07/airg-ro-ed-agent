#!/bin/sh
# Postgres daily backup loop
# Runs once on startup, then daily at 02:00 UTC
set -e

BACKUP_DIR="/backups"
DB_HOST="postgres"
DB_PORT="5432"
DB_USER="ro_ed"
DB_NAME="ro_ed"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

mkdir -p "$BACKUP_DIR"

do_backup() {
    ts="$(date +%Y%m%d_%H%M%S)"
    out="$BACKUP_DIR/ro_ed_${ts}.sql.gz"
    echo "[$(date -Iseconds)] Starting backup -> $out"
    if pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
        --no-owner --clean --if-exists --quote-all-identifiers \
        | gzip > "$out"; then
        size=$(du -h "$out" | cut -f1)
        echo "[$(date -Iseconds)] Backup OK ($size)"
        # Optional S3 upload
        if [ -n "$S3_BACKUP_BUCKET" ]; then
            if command -v aws >/dev/null 2>&1; then
                aws s3 cp "$out" "s3://$S3_BACKUP_BUCKET/$(basename $out)" 2>&1 | tail -2 || echo "S3 upload failed (non-fatal)"
            else
                echo "[s3] aws cli not installed in image - skipping upload"
            fi
        fi
    else
        echo "[$(date -Iseconds)] Backup FAILED" >&2
        rm -f "$out"
        return 1
    fi
}

prune_old() {
    echo "[$(date -Iseconds)] Pruning backups older than ${RETENTION_DAYS}d"
    find "$BACKUP_DIR" -name 'ro_ed_*.sql.gz' -type f -mtime +"$RETENTION_DAYS" -delete
}

# Wait until postgres reachable (max 60s)
i=0
while ! pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -q; do
    i=$((i+1))
    if [ "$i" -gt 60 ]; then
        echo "Postgres not ready after 60s, exiting" >&2
        exit 1
    fi
    sleep 1
done

# Initial backup on startup
do_backup || true
prune_old || true

# Loop: sleep until next 02:00 UTC, run again
while true; do
    now_h=$(date -u +%H)
    now_m=$(date -u +%M)
    # Strip leading zeros so shell arithmetic doesn't treat them as octal
    now_h=$(expr "$now_h" + 0)
    now_m=$(expr "$now_m" + 0)
    target_h=2
    if [ "$now_h" -lt "$target_h" ]; then
        sleep_s=$(( (target_h - now_h) * 3600 - now_m * 60 ))
    else
        sleep_s=$(( (24 - now_h + target_h) * 3600 - now_m * 60 ))
    fi
    [ "$sleep_s" -lt 60 ] && sleep_s=60
    echo "[$(date -Iseconds)] Sleeping ${sleep_s}s until next 02:00 UTC"
    sleep "$sleep_s"
    do_backup || true
    prune_old || true
done
