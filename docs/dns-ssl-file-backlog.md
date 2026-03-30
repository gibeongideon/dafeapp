# DNS / SSL File-by-File Backlog

This backlog turns the DNS / SSL architecture plan into a concrete implementation sequence for the DafeApp codebase.

It is intentionally organized by file so work can be picked up in small, reviewable slices.

Related design document:

- [dns-ssl-implementation-plan.md](dns-ssl-implementation-plan.md)

## Recommended Execution Order

1. Add one platform-managed shared domain configuration for DafeApp itself.
2. Extend domain bindings so each instance can have one primary DafeApp hostname plus optional custom aliases.
3. Update deployment API wiring for platform DNS, custom-domain verification, and Traefik routing.
4. Add bare-metal Traefik installation and dynamic route generation per hostname.
5. Update the UI so instance creation always shows the automatic DafeApp domain and optionally accepts a custom domain.
6. Add reconciliation, health surfacing, and migration helpers.

## Phase 1 — DNS App Foundation

### `dns/models.py`

- [ ] Add `DnsProviderAccount` model for encrypted provider credentials
- [ ] Add `DnsZone` model linked to provider account and organization
- [ ] Add `DnsRecord` model for managed records created by DafeApp
- [ ] Add `DomainAssignment` model or equivalent instance-domain binding model
- [ ] Add provider enums with Cloudflare as the first supported managed provider
- [ ] Add status fields for record lifecycle: `PENDING`, `ACTIVE`, `FAILED`, `DELETED`
- [ ] Add useful indexes for `organization`, `zone`, `hostname`, and `status`

### `dns/admin.py`

- [ ] Register the new DNS models for admin visibility
- [ ] Make sensitive credential fields read-only or masked
- [ ] Add list filters for provider, zone, and record status

### `dns/serializers.py`

- [ ] Add serializers for DNS provider accounts
- [ ] Add serializers for zones
- [ ] Add serializers for records
- [ ] Add serializers for domain assignment state and SSL state

### `dns/views.py`

- [ ] Add CRUD endpoints for provider accounts
- [ ] Add CRUD endpoints for zones
- [ ] Add list/detail endpoints for records
- [ ] Add actions to verify provider credentials and sync zones

### `dns/urls.py`

- [ ] Wire the new DNS endpoints into the `dns` app URLconf

### `dns/tests.py`

- [ ] Add model tests for zone / record uniqueness and lifecycle state
- [ ] Add API tests for provider account creation and validation
- [ ] Add tests for record creation idempotency
- [ ] Add tests for organization scoping and permission enforcement

### New file: `dns/services/__init__.py`

- [ ] Create a service package for DNS provider logic

### New file: `dns/services/base.py`

- [ ] Add a provider abstraction for:
  - credential validation
  - zone listing
  - record upsert
  - record delete
  - record lookup

### New file: `dns/services/cloudflare.py`

- [ ] Implement the Cloudflare provider client
- [ ] Add support for zone discovery
- [ ] Add support for exact record upsert
- [ ] Add support for proxied vs DNS-only records
- [ ] Normalize Cloudflare API errors into clean app-level messages

### New file: `dns/services/factory.py`

- [ ] Add provider resolution logic by account type

## Phase 2 — Deployment Model and API Extensions

### `deployments/models.py`

- [ ] Extend `OdooServer` with domain-routing configuration fields:
  - `managed_dns_enabled`
  - `domain_routing_enabled`
  - `tls_mode`
  - optional relation to DNS zone
- [ ] Extend `OdooInstance` with domain / SSL lifecycle fields:
  - `domain_status`
  - `domain_last_checked_at`
  - `ssl_status`
  - `ssl_error`
- [ ] Add serializer-friendly properties for:
  - `direct_access_url`
  - `domain_access_url`
  - `preferred_access_url`
- [ ] Keep existing `http_port` and `domain` fields intact

### `deployments/serializers.py`

- [ ] Expose new server DNS-routing fields
- [ ] Expose new instance domain and SSL state fields
- [ ] Change access-url serialization to support both direct and domain paths
- [ ] Keep backward compatibility for existing UI consumers where possible

### `deployments/views.py`

- [ ] Extend server create API to accept managed DNS / zone configuration
- [ ] Extend instance create API so bare-metal can accept both `http_port` and optional `domain`
- [ ] Validate domain uniqueness against managed DNS bindings, not just raw string equality
- [ ] Add endpoints for:
  - domain attach
  - domain detach
  - domain retry / reprovision
- [ ] Return richer error messages for DNS and SSL failures

### `deployments/urls.py`

- [ ] Add any new instance domain-management endpoints
- [ ] Add any new server DNS-routing endpoints

### `deployments/tests.py`

- [ ] Add tests for bare-metal instance creation with both port and domain
- [ ] Add tests for server creation with managed DNS options
- [ ] Add tests for serializer output of direct vs preferred access URLs
- [ ] Add tests for domain attach / detach APIs
- [ ] Add tests for invalid zone / hostname combinations

## Phase 3 — Bare-Metal Traefik Bootstrap

### `deployments/tasks.py`

- [ ] Add a task helper to provision managed DNS for an instance
- [ ] Add a task helper to generate or remove a Traefik route for an instance
- [ ] Add a task helper to reconcile server-level domain routing prerequisites
- [ ] Update `configure_odoo_server` so bare-metal servers can install the Traefik gateway when enabled
- [ ] Update `create_odoo_instance` so bare-metal domain-backed instances:
  - keep their assigned host port
  - create DNS
  - create Traefik routing
  - persist domain / SSL status
- [ ] Update delete flow to remove managed DNS records and Traefik route files
- [ ] Add periodic reconciliation for:
  - server IP drift
  - missing DNS records
  - missing Traefik route files
- [ ] Keep SSH reachability checks independent from DNS / SSL checks

### `dafeapp/settings.py`

- [ ] Add any needed DNS / SSL environment flags
- [ ] Add Celery beat schedule entries for DNS / Traefik reconciliation
- [ ] Add settings for Traefik base paths and default TLS mode

### `deployments/tests.py` again

- [ ] Add task-level tests for DNS record provisioning
- [ ] Add task-level tests for Traefik route create / delete
- [ ] Add tests for reconciliation jobs after server IP change

## Phase 4 — Bare-Metal Traefik Ansible Layer

### `infra/ansible/setup_odoo_server_bare.yml`

- [ ] Decide whether to extend this playbook or keep it focused on Odoo environment setup only
- [ ] If extended, add an optional gateway setup branch controlled by extra vars
- [ ] If not extended, keep this file unchanged and use a separate Traefik setup playbook

### New file: `infra/ansible/setup_bare_traefik_gateway.yml`

- [ ] Install Traefik on bare-metal hosts
- [ ] Create `/etc/traefik/`
- [ ] Create `/etc/traefik/dynamic/`
- [ ] Open ports `80` and `443`
- [ ] Start and enable Traefik

### New file: `infra/ansible/templates/traefik-static.yml.j2`

- [ ] Define entrypoints for `80` and `443`
- [ ] Define certificate resolver configuration
- [ ] Define file provider for dynamic instance routes
- [ ] Keep logs and access logs configurable

### New file: `infra/ansible/templates/traefik-dynamic-instance.yml.j2`

- [ ] Render one instance route file per domain-backed instance
- [ ] Route `Host(domain)` to `127.0.0.1:http_port`
- [ ] Include websocket / realtime path handling
- [ ] Include HTTPS router configuration

### New file: `infra/ansible/delete_bare_traefik_route.yml`

- [ ] Remove one per-instance dynamic route file
- [ ] Reload or signal Traefik after route removal

### `infra/ansible/create_odoo_instance.yml`

- [ ] Decide whether this file stays as a legacy `nginx + certbot` path or becomes transitional only
- [ ] If kept for fallback, document clearly that Traefik is the preferred long-term path
- [ ] If replaced, slim it down or retire it in favor of Traefik route generation

### `infra/ansible/create_odoo_instance_direct.yml`

- [ ] Keep the direct-IP instance creation behavior intact
- [ ] Review Odoo proxy-related settings for compatibility with optional Traefik fronting

### `infra/ansible/README.md`

- [ ] Document the new bare-metal Traefik gateway playbooks
- [ ] Document the split between direct access and domain overlay

## Phase 5 — Docker / Traefik Convergence

### `infra/docker/traefik/traefik.yml`

- [ ] Align shared Traefik conventions between Docker and bare-metal where practical
- [ ] Decide whether exact-record rollout stays on HTTP challenge
- [ ] Add future-ready placeholders for DNS challenge without enabling it prematurely

### `infra/docker/docker-compose.base.yml`

- [ ] Add any shared environment conventions needed for future Traefik parity
- [ ] Keep current Docker behavior stable while bare-metal catches up

### `infra/docker/templates/docker-compose.instance.yml.j2`

- [ ] Review label conventions so the Docker routing model and the bare-metal routing model stay conceptually aligned
- [ ] Keep websocket route behavior as the reference implementation for bare-metal

## Phase 6 — UI and UX Updates

### `templates/deployments/create_instance.html`

- [ ] Show the domain field for bare-metal instances as optional, not Docker-only
- [ ] Keep the port field visible for bare-metal even when a domain is entered
- [ ] Add server-level DNS / domain-routing controls in the create-server flow
- [ ] Show both direct and domain access previews where relevant
- [ ] Surface DNS and SSL status on server cards and instance rows
- [ ] Add retry actions for failed domain or SSL provisioning

### `templates/deployments/odoo_instance_console.html`

- [ ] Show both direct URL and domain URL
- [ ] Show preferred URL separately from fallback URL
- [ ] Show DNS record status
- [ ] Show SSL certificate status and errors
- [ ] Add domain attach / detach / reprovision actions if desired in console view

### `deployments/tests.py`

- [ ] Add UI-response tests for new server and instance fields
- [ ] Add regression tests so bare-metal still works without domains

## Phase 7 — Cleanup, Migration, and Backward Compatibility

### `deployments/tasks.py`

- [ ] Add migration helpers for existing instances that already have `domain` set
- [ ] Add logic to backfill preferred access URL fields without breaking old data
- [ ] Add safe fallback behavior if DNS provider credentials are missing

### `deployments/views.py`

- [ ] Add migration or repair endpoints only if admin-triggered repair is needed from UI

### `deployments/tests.py`

- [ ] Add migration/backfill tests for older instances
- [ ] Add tests proving `IP:PORT` remains usable after domain enablement

## Phase 8 — Documentation

### `docs/dns-ssl-implementation-plan.md`

- [ ] Keep the architecture document updated as implementation decisions settle
- [ ] Record any decision to keep or retire nginx-based bare-metal domain support

### `docs/deployment-flow.md`

- [ ] Update the flow once bare-metal Traefik is live
- [ ] Replace transitional wording once the final path is implemented

### `docs/environment-setup.md`

- [ ] Document new Cloudflare environment variables or provider setup expectations
- [ ] Document any Traefik gateway env vars or paths

### `docs/PROGRESS.md`

- [ ] Check off items as each slice lands
- [ ] Keep the rollout order explicit so unfinished fallback paths are visible

## Suggested PR Slices

### PR 1. DNS data layer

- `dns/models.py`
- `dns/admin.py`
- `dns/serializers.py`
- `dns/tests.py`

### PR 2. Cloudflare provider integration

- `dns/services/base.py`
- `dns/services/cloudflare.py`
- `dns/services/factory.py`
- `dns/views.py`
- `dns/urls.py`
- `dns/tests.py`

### PR 3. Deployment model and API extensions

- `deployments/models.py`
- `deployments/serializers.py`
- `deployments/views.py`
- `deployments/urls.py`
- `deployments/tests.py`

### PR 4. Bare-metal Traefik bootstrap

- `infra/ansible/setup_bare_traefik_gateway.yml`
- `infra/ansible/templates/traefik-static.yml.j2`
- `infra/ansible/README.md`
- `deployments/tasks.py`
- `deployments/tests.py`

### PR 5. Per-instance route generation

- `infra/ansible/templates/traefik-dynamic-instance.yml.j2`
- `infra/ansible/delete_bare_traefik_route.yml`
- `deployments/tasks.py`
- `deployments/tests.py`

### PR 6. UI rollout

- `templates/deployments/create_instance.html`
- `templates/deployments/odoo_instance_console.html`
- `deployments/serializers.py`
- `deployments/views.py`
- `deployments/tests.py`

### PR 7. Reconciliation and migration

- `deployments/tasks.py`
- `dafeapp/settings.py`
- `deployments/tests.py`
- `docs/deployment-flow.md`
- `docs/PROGRESS.md`

## Definition of Done

This backlog is complete when:

- a bare-metal instance can be created with only `IP:PORT`
- a bare-metal instance can also be given a managed Cloudflare domain
- Traefik handles HTTPS routing for that bare-metal instance
- the UI shows both access paths and current DNS / SSL state
- deleting an instance cleans up both DNS and routing
- periodic tasks repair drift when DNS or server IP changes
