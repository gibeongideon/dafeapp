# DafeApp — Overview

DafeApp is a **multi-tenant SaaS platform** for deploying and managing Odoo instances on cloud or bare-metal servers. It handles everything from server provisioning (via Terraform + Ansible) to per-instance lifecycle management, all behind an organization-scoped, subscription-gated UI.

---

## What It Does

1. **Connects to infrastructure** — Users add either an external SSH server (PYOS) or a cloud account (DigitalOcean, AWS).
2. **Provisions Odoo servers** — A full Ubuntu 24.04 server is configured via Ansible + custom install scripts.
3. **Creates Odoo instances** — Each instance gets its own PostgreSQL database, systemd service, and optional nginx/SSL config.
4. **Manages the full lifecycle** — Start, stop, delete instances; monitor connectivity; clean up resources.
5. **Multi-org & subscriptions** — Every resource is scoped to an Organization. Subscription plans gate feature access (max instances, backups, staging, etc.).

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Web framework | Django 5.x + Django REST Framework |
| Async / WebSocket | Django Channels 4 + Daphne |
| Task queue | Celery 5 + Redis broker |
| Database | PostgreSQL 16 |
| Cache / Channel layer | Redis 7 |
| Auth | Email+password, JWT, OAuth (Google, GitHub, GitLab) |
| Infrastructure as Code | Terraform (DO/AWS) |
| Configuration management | Ansible |
| Encryption | Fernet (field-level, via `cryptography`) |
| SSH | Paramiko (Ed25519 system keypair) |
| Environment config | django-environ |

---

## Two Infrastructure Paths

```
User selects infrastructure type
         │
         ├── PYOS (external SSH server)
         │       └── Skip Terraform
         │           └── Run Ansible → configure_odoo_server
         │
         └── MANAGED (DigitalOcean / AWS)
                 └── Terraform provisions VM
                     └── Run Ansible → configure_odoo_server
```

Both paths end with the same Ansible playbook installing Odoo on the target server.
