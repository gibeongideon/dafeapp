# DafeApp — Claude Context

DafeApp is a **SaaS platform for provisioning, managing, and monitoring Odoo ERP instances** on cloud or self-hosted infrastructure. Multi-tenant, subscription-gated, async-provisioned via Celery+Ansible+Terraform.

---

## Tech Stack

| Layer | Tech |
|---|---|
| Backend | Django 5.x + DRF + django-environ |
| ASGI | Daphne (Channels 4.x) |
| Auth | django-allauth (Google/GitHub/GitLab) + JWT (simplejwt) |
| Tasks | Celery 5.x + Redis broker + DB results backend |
| Beat | django-celery-beat (DatabaseScheduler) |
| Database | PostgreSQL (via `DATABASE_URL`) |
| Cache/WS | Redis (`REDIS_URL`) |
| IaC | Terraform (~2.0 DO, ~5.0 AWS) + Ansible |
| SSH/crypto | Paramiko + Cryptography (Fernet field encryption) |
| Cloud SDKs | boto3 (AWS) |
| Python | 3.13 (virtualenv at `lvenv/`) |

---

## Project Layout

```
dafeapp/
├── dafeapp/            # Project settings, urls, celery, asgi, routing
├── core/               # Dashboard views, landing page
├── users/              # Custom User model, VCSAccount, social adapters
├── organizations/      # Organization, OrganizationMembership, middleware
├── subscriptions/      # Plan, Subscription, SubscriptionMiddleware
├── tenants/            # Tenant isolation (WIP, mostly empty)
├── cloud/              # ExternalServer (PYOS), CloudAccount (DO/AWS), CloudServer
├── deployments/        # OdooServer, OdooInstance, Infrastructure, DeploymentJob
├── dns/                # DnsZone, DnsRecord, DomainAssignment, DnsProviderAccount
├── backups/            # OdooInstanceBackup, OdooInstanceBackupSchedule
├── monitoring/         # Health checks (WIP, mostly empty)
├── audit/              # AuditLog (40+ action types)
├── infra/
│   ├── ansible/        # All Ansible playbooks
│   ├── terraform/odoo_server/  # Terraform modules (DO + AWS)
│   └── docker/         # Docker Compose templates (Jinja2 .j2)
├── templates/          # Django HTML templates (SSR)
├── docs/               # Markdown documentation
├── scripts/            # Odoo install helper scripts
├── var/                # Runtime data (enterprise archives)
├── Dockerfile          # python:3.13-slim; installs Ansible, Terraform, SSH
├── docker-compose.yml  # dev: db, redis, web, celery_worker, celery_beat
├── docker-compose.prod.yml
├── requirements.txt
└── .env.example        # All env vars documented here
```

---

## Django Apps

### `users`
- Custom `User` (email login, `AbstractUser`): fields `is_platform_admin`, `platform_role`, `is_email_verified`, `auth_provider`, `last_login_ip`, `login_count`
- `VCSAccount`: GitHub/GitLab token per user (Fernet-encrypted)
- Custom allauth adapters (`users/adapters.py`):
  - `AccountAdapter`: post-social redirect → `/dashboard/`
  - `SocialAccountAdapter`: email collision → connect existing user; auto-creates Org for new OAuth users; auto-verifies email

### `organizations`
- `Organization`: `name`, `slug`, `owner→User`, `is_active`
- `OrganizationMembership`: roles `SUPER_ADMIN / ADMIN / MANAGER / USER`; `unique_together (user, organization)`
- `OrganizationMiddleware`: attaches current org to request

### `subscriptions`
- `Plan`: `plan_type` (STARTER/GROWTH/ENTERPRISE), `max_instances`, `max_backups_per_month`, `staging_enabled`, `version_upgrade_enabled`
- `Subscription`: 1:1 with Organization; `status` (ACTIVE/PAST_DUE/CANCELLED/TRIAL/SUSPENDED); `.is_serviceable` property
- `SubscriptionMiddleware`: gates features by plan

### `cloud`
- `ExternalServer` (PYOS — user's own VPS): `host`, `port`, `auth_type` (PASSWORD/DAFEAPP_KEY), `encrypted_password`, `ssh_key_path`, `is_verified`, `is_prepared`, `is_reachable`
- `CloudAccount` (DO/AWS API keys): `provider`, `encrypted_api_token`, DO OAuth token fields, `provider_account_id`, `is_verified`
- `Infrastructure`: ties `ExternalServer` OR `CloudAccount` to an infra record; `infra_type` (PYOS/MANAGED)

### `deployments` — core app
- `OdooServer`: `deployment_mode` (BARE_METAL/DOCKER), `status` (PENDING→PROVISIONED/FAILED/ARCHIVED/DELETED), `odoo_version` (17/18/19), `ip_address`, `platform_domain`, `tls_mode`, `is_reachable`, `agent_token`, `last_heartbeat_at`, `enterprise_shared_path`, `celery_task_id`
- `OdooInstance`: `db_name`, `http_port`, `status` (PENDING/CONFIGURING/RUNNING/STOPPED/FAILED/DELETED), `is_staging`, `parent_instance`, `enterprise_status`, `addons_sync_status`, `container_name`, `base_url`
- `OdooInstanceGitRepo`: Git repo per instance, `encrypted_personal_access_token`, `branch`, `revision`
- `Infrastructure`: scoped to org+server
- `DeploymentJob`: job audit trail (PROVISION_SERVER, CREATE_INSTANCE, etc.)
- `StagingEnvironment`: ephemeral clone of parent instance with `expires_at`
- WebSocket consumers in `deployments/consumers.py` (server, instance, run channels)

### `dns`
- `DnsProviderAccount`: Cloudflare API token (Fernet-encrypted)
- `DnsZone`: `provider_zone_id`, `default_proxied`
- `DnsRecord`: types A/AAAA/CNAME/TXT, `status` (PENDING/ACTIVE/FAILED/DELETED)
- `DomainAssignment`: links instance→zone→record; `source` (PLATFORM/CUSTOM), `is_primary`, `is_managed`

### `backups`
- `OdooInstanceBackup`: `backup_type` (FULL/DB_ONLY), `status` (PENDING/RUNNING/DONE/FAILED), `backup_dir`, `size_bytes`, `branch`/`revision` snapshot

### `audit`
- `AuditLog`: `action` (40+ types: LOGIN, REGISTER, SERVER_ADD, INSTANCE_CREATE…), `metadata` JSON, `ip_address`, `user_agent`; indexed on timestamp/org/user/action

---

## URL Structure

| Prefix | App | Notes |
|---|---|---|
| `/` | landing | |
| `/auth/` | users | login, register, password reset |
| `/accounts/` | allauth | social callbacks |
| `/dashboard/` | core | |
| `/orgs/` | organizations | |
| `/subscriptions/` | subscriptions | |
| `/cloud/` | cloud | dashboard + management |
| `/deployments/` | deployments | UI + API (87 endpoints) |
| `/api/token/` | simplejwt | obtain/refresh/verify |
| `/api/users/` | users | |
| `/api/tenants/` | tenants | |
| `/api/deployments/` | deployments | |
| `/api/dns/` | dns | |
| `/api/backups/` | backups | |
| `/api/monitoring/` | monitoring | |
| `/api/audit/` | audit | |

DRF: JWT auth, `IsAuthenticated` default, `PageNumberPagination` (20), JSONRenderer only.

---

## Celery

**Broker/Results:** Redis / PostgreSQL (django-celery-results)
**Queues:** `celery` (default), `provisioning` (long-running)

### Beat schedule (settings.py)
| Task | Interval |
|---|---|
| `check-server-connectivity` | 180 s |
| `mark-disconnected-servers` | 60 s |
| `repair-stale-heartbeat-agents` | 3600 s |
| `check-instance-health` | 300 s |
| `auto-sync-instance-repos` | 600 s |
| `reconcile-instance-domains` | 300 s |
| `cleanup-expired-staging-instances` | 3600 s |
| `auto-check-core-updates` | 86400 s |

### Key tasks (deployments/tasks.py)
- `provision_odoo_server()` — Terraform + Ansible server provisioning
- `configure_odoo_server()` — server-level Ansible config
- `configure_docker_host()` — Docker host setup (`setup_docker_host.yml`)
- `create_odoo_instance()` — instance creation (bare-metal or Docker)
- `delete_odoo_instance()` — instance teardown
- `provision_instance_domain()` — DNS + domain wiring
- `check_server_connectivity()` — SSH heartbeat
- `reconcile_instance_domains()` — domain state repair

### Key tasks (cloud/tasks.py)
- `validate_external_server()` — PYOS SSH validation (3 retries)
- `prepare_external_server()` — Docker/UFW setup on PYOS

---

## Ansible Playbooks (`infra/ansible/`)

| Playbook | Purpose |
|---|---|
| `setup_odoo_server_bare.yml` | **Active default** — installs Python, PostgreSQL, Nginx, UFW, certbot on Ubuntu |
| `create_odoo_instance_direct.yml` | Creates DB-backed Odoo instance on existing server |
| `delete_odoo_instance_direct.yml` | Removes instance |
| `setup_docker_host.yml` | Docker CE + Traefik v2.11 + PostgreSQL 16-alpine via compose |
| `create_docker_odoo_instance.yml` | Docker-based instance (Traefik labels, healthcheck 300s start_period) |
| `delete_docker_odoo_instance.yml` | Removes Docker instance |
| `setup_bare_traefik_gateway.yml` | Configures Traefik gateway |
| `apply_bare_traefik_route.yml` | Adds Traefik route for domain |
| `delete_bare_traefik_route.yml` | Removes Traefik route |
| `sync_enterprise_to_server.yml` | Two-tier enterprise sync: DafeApp host → server shared dir |
| `sync_odoo_enterprise_addons.yml` | Shared dir → instance path |
| `update_docker_instance_enterprise.yml` | Updates Docker instance enterprise addons |
| `install_dafeapp_heartbeat_agent.yml` | Deploys heartbeat monitoring agent |
| `setup_odoo_server.yml` | Legacy: clone Odoo source (not active) |
| `create_odoo_instance.yml` | Legacy: nginx/systemd instance (not active) |

---

## Terraform (`infra/terraform/odoo_server/`)

- `main.tf`: DigitalOcean Droplets + Firewalls; AWS EC2 + Security Groups
- `variables.tf`: `provider`, `region`, `size`, `name`, `odoo_version`, etc.
- `outputs.tf`: `public_ip`, `instance_id`
- Provider versions: DigitalOcean ~2.0, AWS ~5.0, Random for name suffixes

---

## Deployment Modes

### BARE_METAL (stable, end-to-end working)
Two sub-paths in `deployments/tasks.py`:
1. **PYOS** (user's own VPS via SSH) → skips Terraform → calls `configure_odoo_server` directly
2. **MANAGED** (DO/AWS) → Terraform provisions server → calls `configure_odoo_server`

Reverse proxy: Traefik binary (systemd service)

### DOCKER (working end-to-end as of 2026-04-02)
- Server: `configure_docker_host` → `setup_docker_host.yml`
  - Do NOT pre-create `odoo-network` manually — compose owns it
- Instance: `_run_docker_instance_create` → `create_docker_odoo_instance.yml`
  - psql must use `-d postgres` (not odoo DB)
  - filestore dir must be `owner: "100" group: "101"` (odoo uid/gid)
  - healthcheck `start_period: 300s` — first-boot DB init takes 3–8 min
- Resource limits: `deploy.resources.limits` in `docker-compose.instance.yml.j2`
- HTTP→HTTPS: Traefik labels
- Re-provision failed servers: `POST /api/deployments/odoo/servers/<id>/reprovision/`

---

## Odoo Enterprise Sync (Bare-Metal)
- Two-tier: DafeApp host → `enterprise_shared_path` on server (once per server), then shared path → instance path (fast local copy)
- OdooServer fields: `enterprise_shared_path`, `enterprise_shared_release_code`
- Pre-synced on server provisioning success (non-fatal if fails)
- **TODO (separate session):** Enterprise Docker image — custom build from downloaded enterprise code

---

## WebSocket / Channels

ASGI routing in `dafeapp/routing.py`:

| URL pattern | Consumer | Group key |
|---|---|---|
| `ws/deployments/runs/<run_id>/` | `DeploymentRunConsumer` | `deployments.run.{run_id}` |
| `ws/deployments/servers/<server_id>/` | `OdooServerConsumer` | `odoo.server.{server_id}` |
| `ws/deployments/instances/<instance_id>/` | `OdooInstanceConsumer` | `odoo.instance.{instance_id}` |

Event types: `deployment_update`, `server_update`, `instance_update`, `log_line`

---

## Key Environment Variables

```bash
# Django core
SECRET_KEY=
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=http://localhost:8000
SITE_URL=http://localhost:8000

# DB & cache
DATABASE_URL=postgres://user:pass@localhost:5432/dafeapp
REDIS_URL=redis://localhost:6379/0

# Encryption (Fernet)
FIELD_ENCRYPTION_KEY=

# Email
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.example.com
EMAIL_PORT=587
EMAIL_HOST_USER=
EMAIL_HOST_PASSWORD=
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL=

# Ansible / SSH
ANSIBLE_ODOO_SERVER_PLAYBOOK=setup_odoo_server_bare.yml
ANSIBLE_SSH_USER=ubuntu
ANSIBLE_SSH_KEY_PATH=/path/to/key.pem

# Odoo
ODOO_ADMIN_EMAIL=admin@example.com
DOCKER_POSTGRES_PASSWORD=

# Platform domain auto-routing
PLATFORM_BASE_DOMAIN=
PLATFORM_DNS_PROVIDER=cloudflare
PLATFORM_DNS_API_TOKEN=
PLATFORM_DNS_ZONE_ID=
PLATFORM_DNS_PROXIED=false

# Cloud providers
DIGITALOCEAN_TOKEN=
DIGITALOCEAN_CLIENT_ID=
DIGITALOCEAN_CLIENT_SECRET=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_DEFAULT_REGION=us-east-1
AWS_ROUTE53_ZONE_ID=

# Social OAuth
GOOGLE_CLIENT_ID=
GOOGLE_SECRET=
GITHUB_CLIENT_ID=
GITHUB_SECRET=
GITLAB_CLIENT_ID=
GITLAB_SECRET=
GITLAB_URL=https://gitlab.com

# Celery tuning
CELERY_WORKER_CONCURRENCY=4
SERVER_HEARTBEAT_TIMEOUT_MINUTES=5
```

---

## Common Dev Commands

```bash
# Activate virtualenv
source lvenv/bin/activate

# Run dev server (ASGI)
daphne -b 0.0.0.0 -p 8000 dafeapp.asgi:application

# Migrations
python manage.py makemigrations
python manage.py migrate

# Celery worker (all queues)
celery -A dafeapp worker -Q celery,provisioning -l info

# Celery beat
celery -A dafeapp beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler

# Seed subscription plans
python manage.py seed_plans

# Django shell
python manage.py shell

# Via Docker Compose
docker compose up            # dev stack
docker compose logs -f web   # tail Django logs
docker compose exec web python manage.py migrate
```

---

## Middleware Stack Order

```
SecurityMiddleware
SessionMiddleware
CommonMiddleware
CsrfViewMiddleware
AuthenticationMiddleware
AccountMiddleware          # allauth
OrganizationMiddleware     # attaches current org to request
SubscriptionMiddleware     # gates features by plan
MessageMiddleware
XFrameOptionsMiddleware
```

---

## Security Patterns

- **Field encryption**: Fernet (`FIELD_ENCRYPTION_KEY`) for `encrypted_password`, `encrypted_api_token`, `encrypted_personal_access_token`, `encrypted_aws_*`
- **SSH keys**: stored at paths, not in DB
- **JWT**: simplejwt for API, allauth session for UI
- **CSRF**: enabled for UI views; API uses JWT (exempted)
- **Audit**: every sensitive action written to `AuditLog` with IP + user-agent

---

## Docs

All markdown docs live in `docs/`:
- `overview.md` — architecture overview
- `apps.md` — per-app documentation
- `api-endpoints.md` — full API reference
- `deployment-flow.md` — provisioning flow walkthrough
- `dns-ssl-implementation-plan.md` — DNS/SSL/Traefik design
- `git-addon-manager-plan.md` — multi-repo addon management design
- `environment-setup.md` — local dev + OAuth setup guide
- `PROGRESS.md` — development progress log
