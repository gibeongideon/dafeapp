#!/usr/bin/env bash
# backup.sh — Daily backup of all PostgreSQL databases and Odoo filestores.
#
# Usage:
#   backup.sh [--output-dir /backups] [--retention-days 7]
#
# What it does:
#   1. pg_dump each non-system database in odoo-postgres
#   2. tar.gz each client filestore in /data/odoo/
#   3. Remove backups older than retention-days
#
# Intended to run via cron:
#   0 2 * * * /opt/odoo-docker/scripts/backup.sh >> /var/log/odoo-backup.log 2>&1

set -euo pipefail

OUTPUT_DIR="/backups"
DATA_DIR="/data"
RETENTION_DAYS=7
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir)      OUTPUT_DIR="$2";       shift 2 ;;
    --retention-days)  RETENTION_DAYS="$2";   shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

DB_BACKUP_DIR="${OUTPUT_DIR}/databases/${TIMESTAMP}"
FS_BACKUP_DIR="${OUTPUT_DIR}/filestores/${TIMESTAMP}"
mkdir -p "${DB_BACKUP_DIR}" "${FS_BACKUP_DIR}"

echo "[$(date)] Starting backup — timestamp: ${TIMESTAMP}"

# ── 1. PostgreSQL databases ──────────────────────────────────────────────────
echo "[$(date)] Backing up PostgreSQL databases…"

DATABASES=$(docker exec odoo-postgres psql -U odoo -t -c \
  "SELECT datname FROM pg_database WHERE datistemplate = false AND datname NOT IN ('postgres');" \
  2>/dev/null | tr -d ' ' | grep -v '^$' || true)

if [[ -z "$DATABASES" ]]; then
  echo "[$(date)] No databases found, skipping DB backup."
else
  for DB in $DATABASES; do
    DUMP_FILE="${DB_BACKUP_DIR}/${DB}.sql.gz"
    echo "[$(date)]   Dumping: ${DB} → ${DUMP_FILE}"
    docker exec odoo-postgres pg_dump -U odoo "${DB}" | gzip > "${DUMP_FILE}"
  done
fi

# ── 2. Odoo filestores ────────────────────────────────────────────────────────
echo "[$(date)] Backing up Odoo filestores…"

if [[ -d "${DATA_DIR}/odoo" ]]; then
  for CLIENT_DIR in "${DATA_DIR}/odoo"/*/; do
    CLIENT=$(basename "${CLIENT_DIR}")
    ARCHIVE="${FS_BACKUP_DIR}/${CLIENT}.tar.gz"
    echo "[$(date)]   Archiving: ${CLIENT} → ${ARCHIVE}"
    tar -czf "${ARCHIVE}" -C "${DATA_DIR}/odoo" "${CLIENT}" 2>/dev/null || true
  done
else
  echo "[$(date)] No filestore directory found at ${DATA_DIR}/odoo, skipping."
fi

# ── 3. Remove old backups ─────────────────────────────────────────────────────
echo "[$(date)] Removing backups older than ${RETENTION_DAYS} days…"
find "${OUTPUT_DIR}/databases" -mindepth 1 -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" -exec rm -rf {} + 2>/dev/null || true
find "${OUTPUT_DIR}/filestores" -mindepth 1 -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" -exec rm -rf {} + 2>/dev/null || true

# ── Summary ──────────────────────────────────────────────────────────────────
DB_SIZE=$(du -sh "${DB_BACKUP_DIR}" 2>/dev/null | cut -f1 || echo "0")
FS_SIZE=$(du -sh "${FS_BACKUP_DIR}" 2>/dev/null | cut -f1 || echo "0")
echo "[$(date)] Backup complete — DB: ${DB_SIZE}, Filestores: ${FS_SIZE}"
echo "[$(date)] DB backups:        ${DB_BACKUP_DIR}"
echo "[$(date)] Filestore backups: ${FS_BACKUP_DIR}"
