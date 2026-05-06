#!/bin/bash
# Restore Postgres from a backup file
# Usage: bash scripts/pg_restore.sh <backup_filename.sql.gz>
set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <backup.sql.gz>"
    echo ""
    echo "Available backups:"
    docker exec ro-ed-pg-backup ls -lh /backups | grep ro_ed_ || true
    exit 1
fi

BACKUP_FILE="$1"
echo "WARNING: This will DROP and RESTORE the ro_ed database from $BACKUP_FILE"
read -p "Continue? (yes/no) " confirm
[ "$confirm" = "yes" ] || exit 0

docker exec ro-ed-pg-backup sh -c "
gunzip -c /backups/$BACKUP_FILE | PGPASSWORD=\$PGPASSWORD psql -h postgres -U ro_ed -d ro_ed
"
echo "Restore complete from $BACKUP_FILE"
