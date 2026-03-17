#!/usr/bin/env bash
# delete_instance.sh — Stop and remove a Docker-based Odoo instance.
#
# Usage:
#   delete_instance.sh --client CLIENT_NAME --db DB_NAME [--remove-filestore]
#
# Requirements:
#   - Docker and docker compose plugin installed
#   - odoo-postgres container running (for DB drop)

set -euo pipefail

DOCKER_COMPOSE_DIR="/opt/odoo-docker"
DATA_DIR="/data"
CLIENT_NAME=""
DB_NAME=""
REMOVE_FILESTORE=false

usage() {
  echo "Usage: $0 --client NAME --db DB_NAME [--remove-filestore]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --client)           CLIENT_NAME="$2"; shift 2 ;;
    --db)               DB_NAME="$2";     shift 2 ;;
    --remove-filestore) REMOVE_FILESTORE=true; shift ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

[[ -z "$CLIENT_NAME" || -z "$DB_NAME" ]] && usage

CONTAINER_NAME="odoo-${CLIENT_NAME}"
INSTANCE_COMPOSE="${DOCKER_COMPOSE_DIR}/instances/${CLIENT_NAME}.yml"
FILESTORE_DIR="${DATA_DIR}/odoo/${CLIENT_NAME}"

echo "[1/4] Stopping and removing container via compose…"
if [[ -f "${INSTANCE_COMPOSE}" ]]; then
  docker compose -f "${INSTANCE_COMPOSE}" down --remove-orphans 2>&1 || true
else
  echo "Compose file not found, forcing container removal…"
  docker rm -f "${CONTAINER_NAME}" 2>&1 || true
fi

echo "[2/4] Dropping PostgreSQL database: ${DB_NAME}"
docker exec odoo-postgres psql -U odoo -c "DROP DATABASE IF EXISTS \"${DB_NAME}\";" 2>&1 || true

echo "[3/4] Removing instance compose and conf files…"
rm -f "${INSTANCE_COMPOSE}"
rm -f "${DOCKER_COMPOSE_DIR}/instances/${CLIENT_NAME}.conf"

if [[ "$REMOVE_FILESTORE" == "true" ]]; then
  echo "[4/4] Removing filestore directory: ${FILESTORE_DIR}"
  rm -rf "${FILESTORE_DIR}"
else
  echo "[4/4] Filestore preserved at: ${FILESTORE_DIR} (pass --remove-filestore to delete)"
fi

echo "Instance ${CLIENT_NAME} removed."
