#!/bin/bash
# Trigger one immediate backup
docker exec ro-ed-pg-backup sh -c '
ts="$(date +%Y%m%d_%H%M%S)_manual"
out="/backups/ro_ed_${ts}.sql.gz"
PGPASSWORD="$PGPASSWORD" pg_dump -h postgres -U ro_ed -d ro_ed --no-owner --clean --if-exists | gzip > "$out"
echo "Backup: $out ($(du -h $out | cut -f1))"
'
