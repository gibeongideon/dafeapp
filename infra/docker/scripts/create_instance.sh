#!/usr/bin/env bash
# create_instance.sh — Create a new Docker-based Odoo instance on this host.
#
# Usage:
#   create_instance.sh --client CLIENT_NAME --domain DOMAIN --db DB_NAME \
#                      --version ODOO_VERSION --pg-password PG_PASSWORD \
#                      [--restart-policy POLICY]
#
# Requirements:
#   - Docker and docker compose plugin installed
#   - odoo-network exists (created by setup_docker_host.yml)
#   - odoo-postgres container running with network alias "postgres"

set -euo pipefail

DOCKER_COMPOSE_DIR="/opt/odoo-docker"
DATA_DIR="/data"
RESTART_POLICY="unless-stopped"
CLIENT_NAME=""
DOMAIN=""
DB_NAME=""
ODOO_VERSION=""
PG_PASSWORD=""

usage() {
  echo "Usage: $0 --client NAME --domain DOMAIN --db DB_NAME --version VERSION --pg-password PASSWORD"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --client)         CLIENT_NAME="$2"; shift 2 ;;
    --domain)         DOMAIN="$2";      shift 2 ;;
    --db)             DB_NAME="$2";     shift 2 ;;
    --version)        ODOO_VERSION="$2";shift 2 ;;
    --pg-password)    PG_PASSWORD="$2"; shift 2 ;;
    --restart-policy) RESTART_POLICY="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

[[ -z "$CLIENT_NAME" || -z "$DOMAIN" || -z "$DB_NAME" || -z "$ODOO_VERSION" || -z "$PG_PASSWORD" ]] && usage

CONTAINER_NAME="odoo-${CLIENT_NAME}"
INSTANCE_COMPOSE="${DOCKER_COMPOSE_DIR}/instances/${CLIENT_NAME}.yml"
INSTANCE_CONF="${DOCKER_COMPOSE_DIR}/instances/${CLIENT_NAME}.conf"
FILESTORE_DIR="${DATA_DIR}/odoo/${CLIENT_NAME}"

echo "[1/5] Creating filestore directory: ${FILESTORE_DIR}"
mkdir -p "${FILESTORE_DIR}"
mkdir -p "${DOCKER_COMPOSE_DIR}/instances"

echo "[2/5] Creating PostgreSQL database: ${DB_NAME}"
docker exec odoo-postgres psql -U odoo \
  -c "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" \
  | grep -q 1 \
  || docker exec odoo-postgres psql -U odoo -c "CREATE DATABASE \"${DB_NAME}\";"

echo "[3/5] Writing odoo.conf for instance"
cat > "${INSTANCE_CONF}" << EOF
[options]
addons_path = /mnt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons
data_dir = /var/lib/odoo

db_host = postgres
db_port = 5432
db_user = odoo
db_password = ${PG_PASSWORD}
db_name = ${DB_NAME}

list_db = False
db_filter = ^${DB_NAME}$

proxy_mode = True
gevent_port = 8072

workers = 2
max_cron_threads = 1
limit_memory_hard = 2684354560
limit_memory_soft = 2147483648
limit_time_cpu = 600
limit_time_real = 1200

log_level = warn
EOF

echo "[4/5] Generating docker-compose file: ${INSTANCE_COMPOSE}"
cat > "${INSTANCE_COMPOSE}" << EOF
version: "3.9"

networks:
  odoo-network:
    external: true

services:
  ${CONTAINER_NAME}:
    image: odoo:${ODOO_VERSION}
    container_name: ${CONTAINER_NAME}
    restart: ${RESTART_POLICY}
    command: ["--proxy-mode"]
    environment:
      HOST: postgres
      PORT: "5432"
      USER: odoo
      PASSWORD: "${PG_PASSWORD}"
    volumes:
      - ${FILESTORE_DIR}:/var/lib/odoo
      - ${INSTANCE_CONF}:/etc/odoo/odoo.conf:ro
    networks:
      - odoo-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8069/web/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.${CONTAINER_NAME}.rule=Host(\`${DOMAIN}\`)"
      - "traefik.http.routers.${CONTAINER_NAME}.entrypoints=websecure"
      - "traefik.http.routers.${CONTAINER_NAME}.tls=true"
      - "traefik.http.routers.${CONTAINER_NAME}.tls.certresolver=letsencrypt"
      - "traefik.http.services.${CONTAINER_NAME}.loadbalancer.server.port=8069"
      - "traefik.http.routers.${CONTAINER_NAME}-ws.rule=Host(\`${DOMAIN}\`) && PathPrefix(\`/websocket\`)"
      - "traefik.http.routers.${CONTAINER_NAME}-ws.entrypoints=websecure"
      - "traefik.http.routers.${CONTAINER_NAME}-ws.tls=true"
      - "traefik.http.routers.${CONTAINER_NAME}-ws.tls.certresolver=letsencrypt"
      - "traefik.http.services.${CONTAINER_NAME}-ws.loadbalancer.server.port=8072"
EOF

echo "[5/5] Starting container: ${CONTAINER_NAME}"
docker compose -f "${INSTANCE_COMPOSE}" up -d

echo "Waiting for container to be running…"
for i in $(seq 1 24); do
  STATUS=$(docker inspect --format='{{.State.Status}}' "${CONTAINER_NAME}" 2>/dev/null || echo "missing")
  if [[ "$STATUS" == "running" ]]; then
    echo ""
    echo "Instance ready: https://${DOMAIN}"
    exit 0
  fi
  sleep 5
done

echo "ERROR: Container ${CONTAINER_NAME} did not reach 'running' state in time."
docker logs "${CONTAINER_NAME}" 2>&1 | tail -20
exit 1
