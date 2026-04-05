#!/usr/bin/env bash
# scripts/local-ci.sh
#
# Run the exact same steps that GitHub Actions CI runs, but on your machine.
# Does NOT require sudo. Run as your normal user after adding yourself to the
# docker group once:
#
#   sudo usermod -aG docker $USER
#   newgrp docker           # apply without logging out
#
# Usage (no sudo, no manual virtualenv activation needed):
#   bash scripts/local-ci.sh
#
# Optional — skip tearing down containers after tests (useful for debugging):
#   KEEP_CONTAINERS=1 bash scripts/local-ci.sh

set -euo pipefail

# ── Resolve project root (directory containing this script's parent) ───────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Resolve Python: prefer the project virtualenv, fall back to active env ─────
VENV_PYTHON="$PROJECT_ROOT/lvenv/bin/python"
if [[ -x "$VENV_PYTHON" ]]; then
  PYTHON="$VENV_PYTHON"
elif command -v python &>/dev/null; then
  PYTHON="python"
else
  echo "[CI] ERROR: No Python found. Expected virtualenv at $VENV_PYTHON"
  exit 1
fi

# ── Config ─────────────────────────────────────────────────────────────────────
POSTGRES_CONTAINER="dafeapp_ci_postgres"
REDIS_CONTAINER="dafeapp_ci_redis"
DB_NAME="dafeapp_test"
DB_USER="dafeapp"
DB_PASS="dafeapp"
PG_PORT="5433"   # use 5433 to avoid clash with any local postgres on 5432
REDIS_PORT="6380" # use 6380 to avoid clash with any local redis on 6379
KEEP_CONTAINERS="${KEEP_CONTAINERS:-0}"

# ── Colours ────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[CI]${NC} $*"; }
warn()  { echo -e "${YELLOW}[CI]${NC} $*"; }
error() { echo -e "${RED}[CI]${NC} $*"; }

# ── Cleanup on exit ────────────────────────────────────────────────────────────
cleanup() {
  if [[ "$KEEP_CONTAINERS" == "1" ]]; then
    warn "KEEP_CONTAINERS=1 — leaving postgres + redis running."
    warn "  Stop manually:  docker rm -f $POSTGRES_CONTAINER $REDIS_CONTAINER"
    return
  fi
  info "Stopping CI containers..."
  docker rm -f "$POSTGRES_CONTAINER" "$REDIS_CONTAINER" 2>/dev/null || true
  info "Containers removed."
}
trap cleanup EXIT

# ── 0. Pre-flight checks ───────────────────────────────────────────────────────
info "Checking prerequisites..."

if ! command -v docker &>/dev/null; then
  error "Docker is not installed. Please install Docker first."
  exit 1
fi

if ! "$PYTHON" -c "import django" &>/dev/null; then
  error "Django not found in $PYTHON"
  error "Expected virtualenv at: $VENV_PYTHON"
  exit 1
fi
info "Using Python: $PYTHON"

# ── 1. Start PostgreSQL ────────────────────────────────────────────────────────
info "Starting PostgreSQL container..."
docker rm -f "$POSTGRES_CONTAINER" 2>/dev/null || true
docker run -d \
  --name "$POSTGRES_CONTAINER" \
  -e POSTGRES_DB="$DB_NAME" \
  -e POSTGRES_USER="$DB_USER" \
  -e POSTGRES_PASSWORD="$DB_PASS" \
  -p "${PG_PORT}:5432" \
  postgres:16-alpine

# ── 2. Start Redis ─────────────────────────────────────────────────────────────
info "Starting Redis container..."
docker rm -f "$REDIS_CONTAINER" 2>/dev/null || true
docker run -d \
  --name "$REDIS_CONTAINER" \
  -p "${REDIS_PORT}:6379" \
  redis:7-alpine

# ── 3. Wait for services to be healthy ────────────────────────────────────────
info "Waiting for PostgreSQL to be ready..."
for i in $(seq 1 30); do
  if docker exec "$POSTGRES_CONTAINER" pg_isready -U "$DB_USER" &>/dev/null; then
    info "PostgreSQL is ready."
    break
  fi
  if [[ "$i" -eq 30 ]]; then
    error "PostgreSQL did not become ready in time."
    exit 1
  fi
  sleep 1
done

info "Waiting for Redis to be ready..."
for i in $(seq 1 15); do
  if docker exec "$REDIS_CONTAINER" redis-cli ping | grep -q PONG; then
    info "Redis is ready."
    break
  fi
  if [[ "$i" -eq 15 ]]; then
    error "Redis did not become ready in time."
    exit 1
  fi
  sleep 1
done

# ── 4. Write a temporary CI .env ───────────────────────────────────────────────
# This file mirrors what the GitHub Actions CI step writes.
# It is written to the project root so django-environ picks it up at import time.
# A backup of any existing .env is restored on exit.

ENV_FILE=".env"
ENV_BACKUP=".env.local-ci-backup"

if [[ -f "$ENV_FILE" ]]; then
  warn "Backing up existing .env → $ENV_BACKUP"
  cp "$ENV_FILE" "$ENV_BACKUP"
  # Restore original .env on exit too
  trap 'cleanup; mv -f "$ENV_BACKUP" "$ENV_FILE" 2>/dev/null || true' EXIT
fi

info "Writing CI .env..."
cat > "$ENV_FILE" << EOF
SECRET_KEY=ci-only-insecure-key-do-not-use-in-production
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
SITE_URL=http://localhost:8000
DATABASE_URL=postgres://dafeapp:dafeapp@localhost:5433/dafeapp_test
REDIS_URL=redis://localhost:6380/0
FIELD_ENCRYPTION_KEY=
ANSIBLE_SSH_KEY_PATH=/dev/null
ANSIBLE_SSH_USER=root
ANSIBLE_ODOO_SERVER_PLAYBOOK=/dev/null
ANSIBLE_ODOO_INSTANCE_PLAYBOOK=/dev/null
ANSIBLE_ODOO_INSTANCE_DIRECT_PLAYBOOK=/dev/null
ANSIBLE_ODOO_INSTANCE_DELETE_PLAYBOOK=/dev/null
ANSIBLE_DOCKER_HOST_PLAYBOOK=/dev/null
ANSIBLE_DOCKER_INSTANCE_PLAYBOOK=/dev/null
ANSIBLE_DOCKER_INSTANCE_DELETE_PLAYBOOK=/dev/null
TERRAFORM_SERVER_MODULE_DIR=/tmp
PLATFORM_BASE_DOMAIN=localhost
EOF

# ── 5. Run migrations ──────────────────────────────────────────────────────────
info "Running migrations..."
cd "$PROJECT_ROOT"
"$PYTHON" manage.py migrate --no-input

# ── 6. Run tests ───────────────────────────────────────────────────────────────
info "Running Django tests..."
"$PYTHON" manage.py test --verbosity=2

info "All tests passed."
