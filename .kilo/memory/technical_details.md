# DafeApp Technical Details

## Key Models & Relationships

### Core Models
- **users.User**: Custom user model with email as primary login
- **organizations.Organization**: Multi-tenant organization with slug, auto-generation
- **organizations.OrganizationMembership**: Roles (SUPER_ADMIN, ADMIN, MANAGER, USER)
- **subscriptions.Subscription**: Links organization to plan with status/grace period
- **subscriptions.Plan**: STARTER/GROWTH/ENTERPRISE tiers with feature limits
- **subscriptions.UsageRecord**: Tracks BACKUP/STAGING/UPGRADE events

### Infrastructure Models
- **cloud.CloudAccount**: Encrypted credentials for DigitalOcean/AWS
- **cloud.Infrastructure**: Links org to PYOS or cloud account
- **deployments.OdooServer**: Tracks provisioned servers (status: PENDING→PROVISIONED)
- **deployments.TerraformRun**: Logs terraform commands/outputs/status
- **deployments.OdooInstance**: Tracks Odoo instances (db_name, port, systemd_service, status)

### Git Addon Manager Models
- **deployments.OdooInstanceGitRepo**: Links git repos to instances
- **deployments.OdooInstance** (extended): addon-path fields

### DNS Models
- **dns.DNSProviderAccount**: Cloudflare/DO/Route53 credentials
- **dns.DNSZone**: Managed DNS zones
- **dns.DNSRecord**: Individual DNS records
- **dns.DomainAssignment**: Links domains to servers/instances

### Monitoring & Auditing
- **monitoring.ServerMetrics** (planned): CPU/memory/disk metrics
- **audit.AuditLog**: 26+ action types, org-scoped

## Key Technologies & Patterns

### Infrastructure as Code
- **Terraform**: DigitalOcean + AWS providers in `infra/terraform/`
- **Ansible**: Playbooks in `infra/ansible/playbooks/`
  - `setup_odoo_server_bare.yml`: Ubuntu 24.04 + Odoo install
  - `setup_docker_host.yml`: Docker CE + Traefik + PostgreSQL
  - `create_odoo_instance_*.yml`: Instance creation (bare-metal/docker)
  - `delete_odoo_instance_*.yml`: Instance cleanup

### Async & Real-time
- **Django Channels 4**: WebSocket support for live log streaming
- **Redis**: Channel layer and Celery broker
- **Celery 5**: Task queue with Redis broker and django-db result backend
- **Celery Beat**: Periodic tasks (server connectivity, instance health, etc.)

### Security Patterns
- **Fernet Encryption**: Field-level encryption for cloud credentials/VCS tokens
- **Paramiko**: SSH connectivity with Ed25519 system keypair
- **django-environ**: Environment-based configuration
- **JWT Authentication**: SimpleJWT for API authentication
- **OAuth**: Google/GitHub/GitLab integration via django-allauth

### Multi-tenancy Implementation
- **OrganizationMiddleware**: Sets current org context on every request
- **OrganizationScopedModel**: Base model with org foreign key
- **Context Processors**: Make current org/plan available in templates
- **SubscriptionMiddleware**: Enforces plan limits on requests

## Important Directories

### Apps Structure
- **users/**: Custom user model, profiles, authentication
- **organizations/**: Multi-tenancy, memberships, roles
- **subscriptions/**: Plans, subscriptions, usage tracking
- **cloud/**: Cloud account management (DO/AWS)
- **deployments/**: Server/instance provisioning, Git addon manager
- **dns/**: DNS provider integration and management
- **audit/**: Audit logging
- **monitoring/**: Server/instance monitoring (planned)
- **tenants/**: Tenant isolation (planned)
- **backups/**: Backup management (planned)
- **core/**: Shared utilities, admin configuration

### Infrastructure Code
- **infra/terraform/**: Terraform modules for DO/AWS
- **infra/ansible/**: Playbooks and roles for server configuration
- **infra/docker/**: Docker-specific scripts and templates
- **scripts/**: Standalone deployment scripts

### Templates & Static
- **templates/**: Django templates (organization-scoped UI)
- **staticfiles/**: Collected static assets
- **mediafiles/**: User-uploaded media

## Key API Endpoints (from PROGRESS.md)

### Authentication
- `/auth/login/`: Email/password login
- `/auth/register/`: User registration (auto-creates org)
- `/accounts/<provider>/login/`: OAuth initiation
- `/accounts/<provider>/login/callback/`: OAuth callback

### Organizations
- `/api/organizations/`: CRUD operations
- `/api/memberships/`: Manage org members

### Subscriptions
- `/api/subscriptions/`: Subscription management
- `/api/plans/`: Available plans
- `/api/usage/`: Usage records

### Infrastructure
- `/api/cloud-accounts/`: DigitalOcean/AWS credentials
- `/api/infrastructures/`: PYOS/cloud account linking
- `/api/odoo-servers/`: Server provisioning/detail
- `/api/odoo-instances/`: Instance lifecycle management
- `/api/dns-provider-accounts/`: DNS provider credentials
- `/api/dns-zones/`: DNS zone management
- `/api/dns-records/`: DNS record management
- `/api/domain-assignments/`: Domain to server/instance mapping

### Git Addon Manager
- `/api/odoo-instances/<id>/git-repos/`: List linked repositories
- (Create/edit/delete flows pending)

### Monitoring & Auditing
- `/api/audit-logs/`: Audit log viewing
- (Server metrics endpoints planned)

## Current Development Focus
Based on PROGRESS.md completion status:
- **Completed**: Auth, organizations, subscriptions, cloud infra, server/instance mgmt, Docker mode, deployment reliability
- **In Progress**: DNS & SSL (UI polish), Git Addon Manager (Phase 1 complete)
- **Planned**: Backups, monitoring, advanced instance management, security features, billing

## Environment Variables (.env.example)
Key configuration areas:
- **Django**: SECRET_KEY, DEBUG, ALLOWED_HOSTS
- **Database**: DATABASE_URL (PostgreSQL)
- **Redis**: REDIS_URL
- **Email**: EMAIL_BACKEND, SMTP settings
- **OAuth**: GOOGLE_/GITHUB_/GITLAB_*_ID/SECRET
- **Cloud**: DIGITALOCEAN_/AWS_*_CREDENTIALS
- **Encryption**: FIELD_ENCRYPTION_KEY
- **Traefik**: TRAEFIK_*_SETTINGS (ACME_EMAIL, VERSION, etc.)
- **Platform**: PLATFORM_BASE_DOMAIN, PLATFORM_DNS_*_SETTINGS
- **Celery**: CELERY_WORKER_CONCURRENCY, intervals
- **Odoo**: ODOO_ENTERPRISE_*_ROOT paths