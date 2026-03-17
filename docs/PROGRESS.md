# DafeApp — Progress Tracker

Track of what has been built, what is in progress, and what is planned.

---

## Core Platform

### Authentication & Users

- [x] Custom user model (`users.User`) — email as primary login field
- [x] Email + password login
- [x] Email verification flow
- [x] JWT authentication (DRF SimpleJWT)
- [x] OAuth login — Google, GitHub, GitLab (via django-allauth)
- [x] User registration (auto-creates org)
- [x] Invite-based user onboarding (token + expiry)
- [x] VCS account linking (GitHub/GitLab) with encrypted token storage

### Organizations & Multi-tenancy

- [x] Organization model with slug auto-generation
- [x] Organization membership with roles (SUPER_ADMIN, ADMIN, MANAGER, USER)
- [x] Organization middleware (current org context on every request)
- [x] Org select/switch UI
- [x] Member management (add, remove, change role, activate/deactivate)
- [x] Org-scoped base model and manager (`OrganizationScopedModel`)

### Subscriptions & Billing

- [x] Plan model (STARTER / GROWTH / ENTERPRISE)
- [x] Subscription model with status, grace period logic, auto-renew flag
- [x] UsageRecord model (BACKUP, STAGING, UPGRADE events)
- [x] Subscription middleware (enforces plan limits on requests)
- [ ] Payment gateway integration (Stripe/Paddle)
- [ ] Automated plan upgrades/downgrades
- [ ] Usage-based billing

---

## Cloud Infrastructure

### External Servers (PYOS)

- [x] Add external server (host, port, username, auth type)
- [x] SSH connectivity verification (Paramiko)
- [x] Server preparation (add DafeApp key to authorized_keys)
- [x] DafeApp system SSH keypair (Ed25519, singleton, stored encrypted)
- [x] Public key display UI for users

### Cloud Accounts

- [x] DigitalOcean cloud account (API token, encrypted)
- [x] AWS cloud account (access key + secret, encrypted, region)
- [x] Account verification against provider API
- [x] Available regions/sizes API

### Cloud VMs (Managed)

- [x] CloudServer model (tracks provisioned VMs)
- [x] Droplet provisioning (DigitalOcean)
- [x] Droplet destroy
- [ ] AWS EC2 instance provisioning (model ready, wiring incomplete)

---

## Odoo Deployments

### Server Provisioning

- [x] Infrastructure model (links org to PYOS or managed cloud)
- [x] OdooServer model (full status lifecycle: PENDING → PROVISIONED)
- [x] TerraformRun model (logs command, stdout, stderr, status)
- [x] Terraform module (DigitalOcean + AWS providers)
- [x] Celery task: `provision_odoo_server` (Terraform + Ansible)
- [x] Celery task: `configure_odoo_server` (Ansible playbook runner)
- [x] Ansible playbook: `setup_odoo_server_bare.yml` (Ubuntu 24.04 bare-metal)
- [x] Install script: `odoo_install.sh` (Odoo 17, 18, 19 — configurable)
- [x] PYOS path: skip Terraform, use existing SSH server
- [x] MANAGED path: Terraform provision → Ansible configure
- [x] Periodic connectivity check (Celery Beat, every 2 min)

### Instance Management

- [x] OdooInstance model (db_name, port, systemd_service, status)
- [x] Celery task: `create_odoo_instance` (Ansible)
- [x] Ansible playbook: `create_odoo_instance_direct.yml` (IP:PORT, no nginx)
- [x] Ansible playbook: `create_odoo_instance.yml` (domain + nginx + SSL)
- [x] Celery task: `delete_odoo_instance`
- [x] Ansible playbook: `delete_odoo_instance_direct.yml` (stop, drop DB, close port)
- [x] Instance console/detail view

### Docker Deployment Mode

- [x] `OdooServer.deployment_mode` field (`BARE_METAL` | `DOCKER`)
- [x] `OdooServer.docker_postgres_password` field
- [x] `OdooInstance.container_name` field
- [x] Migration `0007_docker_deployment_mode`
- [x] Task routing: `provision_odoo_server` → `configure_docker_host` when DOCKER
- [x] Task routing: `create_odoo_instance` → Docker path when server is DOCKER
- [x] Task routing: `delete_odoo_instance` → Docker path when server is DOCKER
- [x] Celery task: `configure_docker_host` (installs Docker CE, starts Traefik + PG)
- [x] Ansible: `setup_docker_host.yml` — Docker CE install, `odoo-network`, Traefik + PostgreSQL stack
- [x] Ansible: `create_docker_odoo_instance.yml` — DB create, odoo.conf render, container start
- [x] Ansible: `delete_docker_odoo_instance.yml` — container stop, DB drop, file cleanup
- [x] Docker base compose: Traefik v2.11 + PostgreSQL 16 on `odoo-network`
- [x] PostgreSQL network alias `postgres` (cross-compose hostname resolution)
- [x] PostgreSQL tuning: `shared_buffers=256MB`, `work_mem=16MB`, `max_connections=200`
- [x] `PGDATA` env var set (avoids lost+found mount issue)
- [x] Per-instance `docker-compose.instance.yml.j2` with Traefik labels
- [x] Per-instance `odoo.conf.j2` — `list_db=False`, `db_filter=^<db>$`, `proxy_mode=True`
- [x] Odoo `--proxy-mode` command flag on every container
- [x] Port 8069 Traefik HTTPS routing (main web)
- [x] Port 8072 Traefik HTTPS routing for `/websocket` (live chat / bus)
- [x] Odoo container healthcheck (`/web/health`, `start_period: 60s`)
- [x] Standalone shell scripts: `create_instance.sh`, `delete_instance.sh`, `backup.sh`
- [x] `backup.sh`: `pg_dump` all DBs + filestore tar.gz, configurable retention

### Deployment UI & API

- [x] Odoo server list/create/detail/delete API endpoints
- [x] Odoo instance list/create/delete API endpoints
- [x] Infrastructure CRUD API
- [x] Deployment create view (UI wizard)
- [x] Cloud account options API (regions, sizes)

---

## Observability & Auditing

### Audit Log

- [x] AuditLog model (26+ action types, org-scoped, indexed)
- [x] Audit log viewer (dashboard)
- [ ] Audit log API (endpoint registered, no implementation)
- [ ] Export audit log (CSV/JSON)

### Monitoring

- [x] OdooServer `is_reachable` + `last_checked_at` fields
- [x] Periodic connectivity task (Beat schedule)
- [ ] `monitoring/` app implementation
- [ ] Alerting (email/webhook on server down)
- [ ] Instance-level health checks

---

## Infrastructure Tooling

### Scripts

- [x] `deploy_bare.sh` — standalone SSH deployer (no Django required)
- [x] `create_dns_record.sh` — DNS hook for DO API and Route53
- [x] `test_install/` — Docker test environment for odoo_install.sh
- [x] `infra/docker/scripts/create_instance.sh` — Docker instance creator (standalone)
- [x] `infra/docker/scripts/delete_instance.sh` — Docker instance remover (standalone)
- [x] `infra/docker/scripts/backup.sh` — pg_dump + filestore tar.gz with retention

### DNS

- [x] DNS script (DO + Route53)
- [ ] `dns/` app implementation
- [ ] Automated DNS record creation on server provision

### Backups

- [x] Backup script: `pg_dump` all DBs + filestore tar.gz + retention cleanup
- [ ] `backups/` app implementation (Django-managed schedules)
- [ ] Backup storage (S3 / local)
- [ ] Restore flow

---

## Planned / Future

- [ ] Staging environments (model flag exists: `staging_enabled`)
- [ ] Version upgrades (model flag exists: `version_upgrade_enabled`)
- [ ] Multi-region server orchestration
- [ ] Tenant isolation app (`tenants/`)
- [ ] Full AWS EC2 wiring (Terraform ready, task wiring TBD)
- [ ] API documentation (DRF Spectacular / Swagger)
- [ ] Email backend (production — currently console)

---

## Last Updated

2026-03-17

---

## TODO List — Odoo Deployment (Phased)

> Legend: `[x]` = done · `[ ]` = pending
> Ideas sourced from reference PaaS projects (CapRover, Dokploy, Sidekick).
> Each phase is independently testable before moving to the next.

---

### Phase 1 — Foundation ✅ (Complete)

> Goal: a working end-to-end deploy of an Odoo server and instance.
> Test: provision a server on PYOS or DO, create an instance, access it via IP:PORT.

#### Auth & Users

- [x] Custom user model (email login, JWT, OAuth)
- [x] Email verification flow
- [x] Invite-based user onboarding
- [x] VCS account linking (GitHub/GitLab) with encrypted tokens

#### Organizations

- [x] Organization model with membership roles
- [x] Organization middleware (current org context)
- [x] Member management (add, remove, role, invite)

#### Subscriptions

- [x] Subscription plans model (STARTER / GROWTH / ENTERPRISE)
- [x] Subscription middleware (enforces plan limits)

#### Cloud Accounts & Servers

- [x] External SSH server add / verify / prepare (PYOS)
- [x] DafeApp system SSH keypair (Ed25519, singleton)
- [x] DigitalOcean cloud account add / verify
- [x] AWS cloud account add / verify
- [x] Droplet provisioning and destroy (DigitalOcean)

#### Odoo Server Provisioning

- [x] Infrastructure model (links org to PYOS or cloud account)
- [x] Terraform module (DigitalOcean + AWS)
- [x] Celery task: `provision_odoo_server` (Terraform + Ansible)
- [x] Ansible playbook: `setup_odoo_server_bare.yml` (Ubuntu 24.04)
- [x] Odoo install script (versions 17, 18, 19)
- [x] PYOS path (skip Terraform, use existing SSH server)
- [x] MANAGED path (Terraform provision → Ansible configure)
- [x] Periodic server connectivity check (Celery Beat, every 2 min)

#### Odoo Instance Management

- [x] OdooInstance model + create / delete lifecycle
- [x] Ansible: `create_odoo_instance_direct.yml` (IP:PORT, no nginx)
- [x] Ansible: `create_odoo_instance.yml` (domain + nginx + SSL)
- [x] Ansible: `delete_odoo_instance_direct.yml` (stop, drop DB, close port)

#### Audit

- [x] Audit log model (26+ action types, org-scoped)
- [x] Audit log dashboard viewer

---

### Phase 1b — Docker Deployment Mode ✅ (Complete)

> Goal: run multiple Odoo instances on one server using Docker + Traefik, each on its own domain with automatic HTTPS.
> Test: set `deployment_mode=DOCKER` on a server, create two instances with different domains, verify both reach separate Odoo DBs over HTTPS with valid certs.
>
> Architecture: Internet → Traefik (SSL termination) → Docker network → Odoo containers → shared PostgreSQL

- [x] `OdooServer.deployment_mode` field (`BARE_METAL` | `DOCKER`)
- [x] `OdooServer.docker_postgres_password` field
- [x] `OdooInstance.container_name` field
- [x] Migration `0007_docker_deployment_mode`
- [x] Task routing: `provision_odoo_server` → `configure_docker_host` when DOCKER
- [x] Task routing: `create_odoo_instance` → Docker path when server is DOCKER
- [x] Task routing: `delete_odoo_instance` → Docker path when server is DOCKER
- [x] Celery task: `configure_docker_host` (installs Docker CE, starts Traefik + PG)
- [x] Ansible: `setup_docker_host.yml` — Docker CE install, `odoo-network`, Traefik + PostgreSQL stack
- [x] Ansible: `create_docker_odoo_instance.yml` — DB create, odoo.conf render, container start
- [x] Ansible: `delete_docker_odoo_instance.yml` — container stop, DB drop, file cleanup
- [x] Docker base compose: Traefik v2.11 + PostgreSQL 16 on `odoo-network`
- [x] PostgreSQL network alias `postgres` (cross-compose hostname resolution)
- [x] PostgreSQL tuning: `shared_buffers=256MB`, `work_mem=16MB`, `max_connections=200`
- [x] `PGDATA` env var set (avoids lost+found mount issue)
- [x] Per-instance `docker-compose.instance.yml.j2` with Traefik labels
- [x] Per-instance `odoo.conf.j2` — `list_db=False`, `db_filter=^<db>$`, `proxy_mode=True`
- [x] Odoo `--proxy-mode` command flag on every container
- [x] Port 8069 Traefik HTTPS routing (main web)
- [x] Port 8072 Traefik HTTPS routing for `/websocket` (live chat / bus)
- [x] Odoo container healthcheck (`/web/health`, `start_period: 60s`)
- [x] Standalone shell scripts: `create_instance.sh`, `delete_instance.sh`, `backup.sh`
- [x] `backup.sh`: `pg_dump` all DBs + filestore tar.gz, configurable retention

---

### Phase 2 — Deployment Reliability ✅

> Goal: make deployments observable, recoverable, and self-healing.
> Test: watch live logs during provision, trigger a health-fail, roll back, restart automatically.

- [x] Deployment job queue with status tracking and cancellation (`DeploymentJob` model + cancel endpoint)
- [x] Real-time deployment log streaming via WebSocket (Ansible Popen streaming → `log.line` WS event)
- [x] Instance health check endpoint (Odoo `/web` ping — manual + periodic every 5 min)
- [x] Instance restart policy configuration (always / on-failure — field on `OdooInstance`, passed to Ansible)
- [x] Version history tracking per OdooServer (`OdooServerHistory` snapshot on successful provision)
- [x] Version history tracking per OdooInstance (`OdooInstanceHistory` snapshot on successful create/rollback)
- [x] Rollback to previous instance version / snapshot (`rollback_odoo_instance` task + API endpoint)
- [ ] AWS EC2 instance provisioning (Terraform ready, task wiring incomplete)

---

### Phase 3 — DNS & SSL

> Goal: every instance gets a proper domain and auto-renewing SSL cert.
> Test: provision an instance, verify DNS record appears and HTTPS works end-to-end.

- [x] DNS scripts (DigitalOcean API + Route53)
- [x] Traefik automatic HTTPS via Let's Encrypt (Docker mode)
- [ ] Automated DNS record creation on server / instance provision
- [ ] Let's Encrypt certificate auto-renewal (bare-metal / nginx path)
- [ ] Custom certificate upload per instance
- [ ] Domain management UI (add / remove domains per instance)

---

### Phase 4 — Backups & Disaster Recovery

> Goal: every instance is backed up on a schedule and can be restored.
> Test: schedule a backup, delete the DB, restore from backup, verify Odoo starts clean.

- [x] Backup script: `pg_dump` all DBs + filestore tar.gz + retention (`backup.sh`)
- [ ] `backups/` app implementation (Django-managed schedules)
- [ ] S3-compatible backup destination management (DO Spaces, AWS S3)
- [ ] Backup retention policy (keep N latest)
- [ ] Database restore workflow
- [ ] Volume / filestore restore

---

### Phase 5 — Monitoring & Alerting

> Goal: know when a server is struggling before users notice.
> Test: spike CPU on a server, verify alert fires to email and Slack within the threshold window.

- [x] OdooServer `is_reachable` + `last_checked_at` fields
- [x] Periodic connectivity check (Beat schedule)
- [ ] Real-time CPU / memory / disk metrics per server
- [ ] Per-instance metrics (Odoo service resource usage)
- [ ] Configurable alert thresholds (CPU %, memory %)
- [ ] Multi-channel notifications (email, Slack, Telegram, webhook)
- [ ] Server down / up alerting

---

### Phase 6 — Advanced Instance Management

> Goal: production-grade instance control — zero downtime, upgrades, resource isolation.
> Test: upgrade Odoo version with no downtime; spin up a staging clone; deploy a custom addon via git push.

- [ ] Zero-downtime deployment (blue-green swap)
- [ ] Odoo version upgrade workflow (in-place upgrade)
- [ ] Pre-deploy hooks (custom scripts run before Odoo starts)
- [ ] Resource limits per instance (CPU and memory reservation / limit)
- [ ] HTTP Basic Auth per instance
- [ ] Staging environment (clone instance to staging slot)
- [ ] Preview deployments (branch-specific ephemeral instances)
- [ ] Custom addons management (upload, install, version tracking)
- [ ] Git-based addons auto-pull (webhook trigger on push)
- [ ] Shared addons mount across multiple instances on same server

---

### Phase 7 — Security & API Access

> Goal: platform is safe for team use and scriptable via API.
> Test: create an API key, trigger a deployment via CI, verify 2FA blocks unauthorized login.

- [ ] Two-factor authentication (TOTP)
- [ ] API keys for automation (per-org, with rate limits)
- [ ] Deploy-only tokens (no admin access, for CI/CD)
- [ ] SSH key management per org (team keys, upload/rotate)
- [ ] Audit log API endpoint
- [ ] Export audit log (CSV / JSON)
- [ ] Instance-level log viewer (systemd journal streaming)
- [ ] Log retention and auto-cleanup policy

---

### Phase 8 — Billing & Business

> Goal: the platform can charge customers and enforce paid limits.
> Test: upgrade a plan via Stripe checkout, verify new instance limits apply immediately.

- [ ] Payment gateway integration (Stripe)
- [ ] Automated plan upgrades / downgrades
- [ ] Usage-based billing

---

### Phase 9 — Future / Advanced

> Goal: expand deployment targets and developer experience.
> No fixed test — each item is self-contained.

- [ ] One-click Odoo configuration templates (CRM, eCommerce, etc.)
- [ ] API documentation (DRF Spectacular / Swagger)
- [ ] Production email backend
