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

### DNS
- [x] DNS script (DO + Route53)
- [ ] `dns/` app implementation
- [ ] Automated DNS record creation on server provision

### Backups
- [ ] `backups/` app implementation
- [ ] Scheduled backup tasks
- [ ] Backup storage (S3 / local)
- [ ] Restore flow

---

## Planned / Future

- [ ] Docker-based Odoo deployment (bare-metal only for now)
- [ ] Staging environments (model flag exists: `staging_enabled`)
- [ ] Version upgrades (model flag exists: `version_upgrade_enabled`)
- [ ] Multi-region server orchestration
- [ ] Tenant isolation app (`tenants/`)
- [ ] WebSocket real-time provisioning logs
- [ ] Full AWS EC2 wiring (Terraform ready, task wiring TBD)
- [ ] API documentation (DRF Spectacular / Swagger)
- [ ] Email backend (production — currently console)

---

## Last Updated
2026-03-12
