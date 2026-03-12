# API Endpoints

## Authentication

All API endpoints require a JWT Bearer token unless noted.

```
POST /api/token/              — Obtain JWT (email + password)
POST /api/token/refresh/      — Refresh access token
POST /api/token/verify/       — Verify token validity
```

---

## Users API

```
POST   /api/users/register/   — Register new user (creates org)
GET    /api/users/me/         — Get current user profile
PUT    /api/users/me/         — Update profile
GET    /api/users/            — List org members
PUT    /api/users/<id>/role/  — Update a member's role
```

---

## Deployments API

### Infrastructure
```
GET    /deployments/infrastructure/           — List org infrastructures
POST   /deployments/infrastructure/create/    — Create infrastructure record
DELETE /deployments/infrastructure/<id>/delete/
```

### Odoo Servers
```
GET    /deployments/odoo/servers/             — List servers
POST   /deployments/odoo/servers/create/      — Provision new Odoo server (triggers Celery task)
GET    /deployments/odoo/servers/<id>/        — Server detail + status
DELETE /deployments/odoo/servers/<id>/delete/ — Delete server
GET    /deployments/odoo/servers/<id>/check/  — Check connectivity
```

### Odoo Instances
```
GET    /deployments/odoo/instances/           — List instances on org's servers
POST   /deployments/odoo/instances/create/    — Create new instance (triggers Celery task)
GET    /deployments/odoo/instances/<id>/      — Instance console/detail
DELETE /deployments/odoo/instances/<id>/delete/
```

### PYOS VPS
```
POST   /deployments/pyos/vps/create/          — Provision on PYOS server (no Terraform)
```

### Cloud Options & Runs
```
GET    /deployments/options/<account_id>/     — Available regions/sizes for a cloud account
GET    /deployments/instances/<id>/           — Cloud instance detail (Terraform output)
GET    /deployments/runs/<run_id>/            — Terraform run log and status
```

---

## UI Routes (template-rendered)

### Auth (`/auth/`)
```
GET/POST  /auth/login/
GET       /auth/logout/
GET/POST  /auth/register/
GET       /auth/verify-email/<token>/
GET/POST  /auth/invite/<token>/
POST      /auth/vcs/<id>/disconnect/
```

### Dashboard (`/dashboard/`)
```
GET  /dashboard/              — Home
GET  /dashboard/connections/  — Connections overview
GET  /dashboard/profile/      — User profile
GET  /dashboard/users/        — Org user management
GET  /dashboard/audit/        — Audit log
GET  /dashboard/vcs/          — VCS account management
GET  /dashboard/docs/installation/  — Installation docs
```

### Organizations (`/orgs/`)
```
GET  /orgs/select/
GET  /orgs/switch/<id>/
GET  /orgs/members/
POST /orgs/members/<id>/role/
POST /orgs/members/<id>/toggle/
POST /orgs/members/<id>/remove/
```

### Cloud (`/cloud/`)
```
GET       /cloud/                            — Cloud dashboard
GET/POST  /cloud/servers/add/               — Add external server
GET       /cloud/servers/<id>/              — Server detail
POST      /cloud/servers/<id>/verify/       — Verify SSH connection
POST      /cloud/servers/<id>/prepare/      — Prepare server (key auth)
GET/POST  /cloud/accounts/add/              — Add cloud account
POST      /cloud/accounts/<id>/verify/      — Verify cloud credentials
GET       /cloud/accounts/<id>/options/     — Regions/sizes for account
POST      /cloud/droplets/provision/        — Provision droplet
POST      /cloud/droplets/<id>/destroy/     — Destroy droplet
GET       /cloud/ssh-key/                   — Show DafeApp public SSH key
```

---

## Stub APIs (registered but no logic yet)

```
/api/dns/
/api/backups/
/api/monitoring/
/api/audit/
```

---

## OAuth (allauth)

```
/accounts/google/login/
/accounts/github/login/
/accounts/gitlab/login/
/accounts/...            — allauth standard routes
```
