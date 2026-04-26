# DafeApp Project Summary

## Overview
DafeApp is a multi-tenant SaaS platform for deploying and managing Odoo instances on cloud or bare-metal servers. It provides end-to-end infrastructure automation from server provisioning to instance lifecycle management, with organization-scoped access and subscription-based feature gating.

## Core Functionality
1. **Infrastructure Provisioning**:
   - Supports external SSH servers (PYOS) or cloud accounts (DigitalOcean/AWS)
   - Automated Ubuntu 24.04 server setup via Terraform + Ansible
   - Odoo installation with configurable versions (17, 18, 19)

2. **Instance Management**:
   - Create/delete Odoo instances with dedicated PostgreSQL databases
   - Support for both bare-metal (direct install) and Docker deployment modes
   - Domain-based routing with Traefik + Let's Encrypt SSL (optional)
   - Full lifecycle: start, stop, monitor, backup/restore

3. **Multi-tenancy & Security**:
   - Organization model with role-based access (SUPER_ADMIN, ADMIN, MANAGER, USER)
   - Subscription plans (STARTER/GROWTH/ENTERPRISE) gating resource limits
   - JWT authentication, OAuth (Google/GitHub/GitLab), encrypted credential storage
   - Field-level encryption for sensitive data using Fernet

4. **Git Addon Manager** (In Progress):
   - Link Git repositories to Odoo instances for custom addon management
   - Phased implementation: visibility → cloning → branch switching → auth → automation

## Technical Stack
- **Backend**: Django 5.x + Django REST Framework
- **Real-time**: Django Channels 4 + Daphne (WebSockets for live logs)
- **Task Queue**: Celery 5 + Redis broker + django-db result backend
- **Database**: PostgreSQL 16
- **Cache**: Redis 7 (channel layer and Celery broker)
- **Infrastructure**: Terraform (DO/AWS), Ansible
- **Security**: Fernet encryption, Paramiko SSH, django-environ
- **Authentication**: Email/password, JWT, OAuth via django-allauth

## Architecture Highlights
- **Multi-tenancy**: OrganizationMiddleware + OrganizationScopedModel base class
- **Deployment Paths**:
  - PYOS: Skip Terraform → Run Ansible configure_odoo_server
  - MANAGED: Terraform provisions VM → Run Ansible configure_odoo_server
- **Docker Mode**: Traefik + Docker network for multi-instance isolation with HTTPS
- **Async Operations**: Celery tasks for provisioning, health checks, DNS reconciliation
- **Real-time Features**: WebSocket streaming for deployment logs via Django Channels

## Current Progress (from PROGRESS.md)
### Completed Features:
- ✅ Authentication & Users (custom model, email login, OAuth, JWT)
- ✅ Organizations & Multi-tenancy (roles, middleware, membership management)
- ✅ Subscriptions & Billing (plan models, usage tracking, middleware enforcement)
- ✅ Cloud Infrastructure (PYOS/DigitalOcean accounts, server provisioning)
- ✅ Odoo Server/Instance Management (full lifecycle, Docker mode)
- ✅ Deployment Reliability (job tracking, log streaming, health checks, rollback)
- ✅ DNS & SSL (Cloudflare integration, Traefik routing, Let's Encrypt)
- ✅ Git Addon Manager Phase 1 (data model, UI visibility, read-only API)

### In Progress:
- ⏳ Git Addon Manager Phases 2-5 (cloning, branch switching, auth, automation)
- ⏳ Backups app (Django-managed schedules, S3 storage, restore workflow)
- ⏳ Monitoring & Alerting (real-time metrics, notifications)
- ⏳ Advanced Instance Management (zero-downtime upgrades, staging, resource limits)
- ⏳ Security & API Access (2FA, API keys, audit log export)
- ⏳ Billing & Business (Stripe integration, automated plan changes)

## Key Directories
- **apps/**: Django apps organized by concern (users, organizations, subscriptions, deployments, etc.)
- **infra/**: Infrastructure-as-code (Terraform modules, Ansible playbooks)
- **scripts/**: Standalone deployment helpers
- **templates/**: Organization-scoped Django templates
- **.kilo/**: Kilo CLI configuration and memory storage

## Environment Configuration
Key settings in `.env` file:
- Django: SECRET_KEY, DEBUG, ALLOWED_HOSTS
- Database: DATABASE_URL (PostgreSQL)
- Redis: REDIS_URL
- Email: SMTP credentials for production
- OAuth: Client IDs/secrets for Google/GitHub/GitLab
- Cloud: Encrypted credentials for DigitalOcean/AWS
- Encryption: FIELD_ENCRYPTION_KEY for sensitive data
- Traefik: ACME email, version, TLS settings
- Platform: Base domain, DNS provider settings
- Celery: Worker concurrency, task intervals

## Development Status
As of the latest update (2026-03-29), the platform is approximately 50% complete with core functionality implemented and several advanced features in various stages of development. The foundation for multi-tenant Odoo deployment is solid, with ongoing work focused on enhancing the Git addon manager, backup systems, monitoring, and billing integration.