#!/usr/bin/env bash
# scripts/droplet-setup.sh
#
# One-time setup script for a fresh DigitalOcean Droplet (Ubuntu 22.04 / 24.04).
# Run this ONCE via SSH after you create the Droplet:
#
#   ssh root@YOUR_DROPLET_IP "bash -s" < scripts/droplet-setup.sh
#
# After this runs:
#   1. Docker + Docker Compose (plugin) are installed
#   2. /opt/dafeapp/ is created
#   3. A .env template is written — fill in real values before first deploy

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[setup]${NC} $*"; }
warn() { echo -e "${YELLOW}[setup]${NC} $*"; }

# ── 1. System update ───────────────────────────────────────────────────────────
info "Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq

# ── 2. Install Docker CE ───────────────────────────────────────────────────────
info "Installing Docker CE..."
apt-get install -y -qq ca-certificates curl gnupg lsb-release

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Enable + start Docker
systemctl enable docker
systemctl start docker

info "Docker version: $(docker --version)"
info "Docker Compose version: $(docker compose version)"

# ── 3. Create app directory ────────────────────────────────────────────────────
info "Creating /opt/dafeapp/..."
mkdir -p /opt/dafeapp
cd /opt/dafeapp

# ── 4. Generate a Fernet key if not already set ────────────────────────────────
FERNET_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null || echo "GENERATE_MANUALLY")

# ── 5. Write .env template ─────────────────────────────────────────────────────
info "Writing /opt/dafeapp/.env template..."
cat > /opt/dafeapp/.env << ENVEOF
# ── Django ─────────────────────────────────────────────────────────────────────
SECRET_KEY=CHANGE_ME_generate_with_python_-c_"import_secrets;print(secrets.token_hex(50))"
DEBUG=False
ALLOWED_HOSTS=YOUR_DROPLET_IP_OR_DOMAIN
SITE_URL=http://YOUR_DROPLET_IP_OR_DOMAIN:8000
SESSION_COOKIE_SECURE=False
CSRF_COOKIE_SECURE=False

# ── Database (matches docker-compose.prod.yml defaults) ───────────────────────
DATABASE_URL=postgres://dafeapp:CHANGE_ME_DB_PASSWORD@db:5432/dafeapp
DB_NAME=dafeapp
DB_USER=dafeapp
DB_PASSWORD=CHANGE_ME_DB_PASSWORD

# ── Redis ──────────────────────────────────────────────────────────────────────
REDIS_URL=redis://redis:6379/0

# ── Field-level encryption ─────────────────────────────────────────────────────
# Auto-generated below — copy it out and keep it safe.
FIELD_ENCRYPTION_KEY=${FERNET_KEY}

# ── Ansible / Terraform paths (absolute paths inside the container) ────────────
ANSIBLE_SSH_KEY_PATH=/app/infra/ssh/dafeapp_id_ed25519
ANSIBLE_SSH_USER=root
ANSIBLE_ODOO_SERVER_PLAYBOOK=/app/infra/ansible/setup_odoo_server_bare.yml
ANSIBLE_ODOO_INSTANCE_PLAYBOOK=/app/infra/ansible/create_odoo_instance.yml
ANSIBLE_ODOO_INSTANCE_DIRECT_PLAYBOOK=/app/infra/ansible/create_odoo_instance_direct.yml
ANSIBLE_ODOO_INSTANCE_DELETE_PLAYBOOK=/app/infra/ansible/delete_odoo_instance_direct.yml
ANSIBLE_DOCKER_HOST_PLAYBOOK=/app/infra/ansible/setup_docker_host.yml
ANSIBLE_DOCKER_INSTANCE_PLAYBOOK=/app/infra/ansible/create_docker_odoo_instance.yml
ANSIBLE_DOCKER_INSTANCE_DELETE_PLAYBOOK=/app/infra/ansible/delete_docker_odoo_instance.yml
TERRAFORM_SERVER_MODULE_DIR=/app/infra/terraform/odoo_server

# ── Cloud credentials ──────────────────────────────────────────────────────────
DIGITALOCEAN_TOKEN=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_DEFAULT_REGION=us-east-1
AWS_ROUTE53_ZONE_ID=

# ── DNS ────────────────────────────────────────────────────────────────────────
PLATFORM_BASE_DOMAIN=dafeapp.com
DNS_PROVIDER=
DNS_ROOT_DOMAIN=

# ── OAuth ──────────────────────────────────────────────────────────────────────
GITHUB_CLIENT_ID=
GITHUB_SECRET=
GOOGLE_CLIENT_ID=
GOOGLE_SECRET=
GITLAB_CLIENT_ID=
GITLAB_SECRET=
ENVEOF

chmod 600 /opt/dafeapp/.env

warn "------------------------------------------------------------"
warn " /opt/dafeapp/.env has been created."
warn " Edit it now and fill in all CHANGE_ME / blank values:"
warn "   nano /opt/dafeapp/.env"
warn ""
warn " Key things to set:"
warn "   SECRET_KEY          — random 50-char hex string"
warn "   ALLOWED_HOSTS       — your droplet IP or domain"
warn "   SITE_URL            — http(s)://your-ip-or-domain:8000"
warn "   DB_PASSWORD         — strong password (also update DATABASE_URL)"
warn "   FIELD_ENCRYPTION_KEY — already generated above, back it up!"
warn "   DIGITALOCEAN_TOKEN  — your DO API token"
warn "------------------------------------------------------------"

info "Droplet setup complete. Next steps:"
info "  1. Edit /opt/dafeapp/.env with your real values"
info "  2. Add the following GitHub Secrets to your repository:"
info "       DO_HOST     = $(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_DROPLET_IP')"
info "       DO_USER     = root"
info "       DO_SSH_KEY  = (contents of your SSH private key)"
info "       GHCR_TOKEN  = (GitHub PAT with read:packages scope)"
info "       FIELD_ENCRYPTION_KEY = (the key printed in .env above)"
info "  3. Push to the dev branch — the deploy workflow will run automatically."
