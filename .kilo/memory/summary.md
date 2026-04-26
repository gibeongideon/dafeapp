# DafeApp Project Summary

## What It Does
DafeApp is a multi-tenant SaaS platform for deploying and managing Odoo instances on cloud or bare-metal servers. It handles:
- Server provisioning (via Terraform + Ansible)
- Per-instance lifecycle management
- Organization-scoped, subscription-gated UI

## Key Features
1. **Infrastructure Management**:
   - Connect to external SSH servers (PYOS) or cloud accounts (DigitalOcean, AWS)
   - Provision Ubuntu 24.04 servers via Ansible + custom install scripts
   - Create Odoo instances with dedicated PostgreSQL databases, systemd services, and IP:PORT access
   - Domain-based routing with Traefik and Let's Encrypt SSL
   - Full lifecycle management: start, stop, delete instances; monitor connectivity

2. **Multi-tenancy & Security**:
   - Organization model with role-based access (SUPER_ADMIN, ADMIN, MANAGER, USER)
   - Organization middleware for request scoping
   - Subscription plans (STARTER/GROWTH/ENTERPRISE) gating feature access
   - JWT authentication, OAuth (Google/GitHub/GitLab), encrypted credential storage

3. **Technical Stack**:
   - Backend: Django 5.x + Django REST Framework
   - Real-time: Django Channels 4 + Daphne
   - Task Queue: Celery 5 + Redis broker
   - Database: PostgreSQL 16
   - Cache: Redis 7
   - Infrastructure: Terraform, Ansible
   - Security: Fernet encryption, Paramiko SSH, django-environ

4. **Deployment Modes**:
   - Bare-metal: Direct Odoo installation with optional Traefik reverse proxy
   - Docker: Multiple instances per server using Docker + Traefik, each with HTTPS domains

5. **Git Addon Manager** (In Progress):
   - Link Git repositories to Odoo instances
   - Manage custom addons via GitHub/GitLab
   - Phased implementation: visibility → cloning → branch switching → auth → automation

## Current Progress
As per PROGRESS.md (last updated 2026-03-29):
- Authentication, organizations, subscriptions: Complete
- Cloud infrastructure (PYOS/DigitalOcean): Complete
- Odoo server/instance management: Complete
- Docker deployment mode: Complete
- Phase 2 (Deployment Reliability): Complete
- Phase 3 (DNS & SSL): Mostly complete (UI polish remaining)
- Phase 4+ (Backups, Monitoring, Advanced Features): In various stages of completion
- Git Addon Manager: Phase 1 complete (foundation/UI), Phase 2-5 pending

## Infrastructure Paths
1. **PYOS (External Server)**: Skip Terraform → Run Ansible
2. **MANAGED (Cloud)**: Terraform provisions VM → Run Ansible

Both paths converge on the same Ansible playbook for Odoo server configuration.