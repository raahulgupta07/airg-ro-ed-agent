# Operations Scripts

## Backups
- `pg_backup_loop.sh` — Service entrypoint (auto-runs in `pg-backup` container)
- `pg_backup_now.sh`  — Trigger immediate backup
- `pg_restore.sh <f>` — Restore from a backup file

## TLS
- `generate_dev_cert.sh` — Self-signed cert for local dev

## Backup retention
14 days default. Override via `RETENTION_DAYS` env in `docker-compose.yml`.

## Backup location
Inside docker volume `pg-backups`. Browse:
```
docker exec ro-ed-pg-backup ls -lh /backups
```

## Backup schedule
- One backup runs immediately on container startup.
- After that, one backup per day at 02:00 UTC.

## Optional S3 upload
Set `S3_BACKUP_BUCKET` env on the `pg-backup` service to push each backup to
`s3://$S3_BACKUP_BUCKET/<filename>`.

**Limitation:** the default `postgres:16-alpine` image does **not** include the
`aws` CLI. To enable S3 uploads, either:
- switch the `pg-backup` image to `postgres:16` (Debian) and `apt-get install awscli`, or
- build a custom image: `FROM postgres:16-alpine` + `RUN apk add --no-cache aws-cli`.

If `aws` is missing, the backup loop logs `[s3] aws cli not installed in image - skipping upload`
and continues normally (non-fatal).
