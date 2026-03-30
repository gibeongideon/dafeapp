# DNS / SSL Implementation Plan

This document captures the recommended path for adding first-class domain management and SSL to DafeApp without breaking the current `IP:PORT` flow that already works well for bare-metal and PYOS deployments.

Companion backlog:

- [dns-ssl-file-backlog.md](dns-ssl-file-backlog.md)

## Goal

Keep the current Odoo provisioning logic intact, keep direct `IP:PORT` access working, and add a stronger domain-based path on top using:

- Traefik for routing and TLS termination
- Cloudflare for DNS management and optional proxying
- DafeApp's own `dns/` app for domain lifecycle, instead of ad hoc shell hooks

Primary product requirement:

- every instance gets an automatic DafeApp-managed hostname under one shared platform domain such as `app1.dafeapp.com`
- an instance may also attach one or more custom hostnames from external DNS providers
- both the platform hostname and custom hostname(s) should work for the same instance at the same time
- per-organization DNS accounts are not the primary architecture for the current rollout

## Current Project State

### What already exists

- `OdooServer` already stores `ip_address` and `dns_domain`.
- `OdooInstance` already stores both `domain` and `http_port`.
- Bare-metal instance creation already supports two playbook paths:
  - direct `IP:PORT` via `create_odoo_instance_direct.yml`
  - domain-based deployment via `create_odoo_instance.yml`
- Docker mode already uses Traefik for HTTPS routing per domain.

### What is still missing

- The UI only treats domains as a Docker concern.
- Bare-metal instances do not expose domain management as a first-class flow.
- The `dns/` app is still a placeholder and does not manage provider-backed DNS.
- Bare-metal domain routing still uses `nginx + certbot` instead of Traefik.
- Access URLs are modeled mainly around direct `IP:PORT`.

## Recommended Architecture

### 1. Preserve the current runtime model

Do not replace the current bare-metal Odoo model.

- Odoo should continue running as systemd services bound to dedicated host ports.
- Direct access via `http://SERVER_IP:PORT` should remain valid.
- Domain-based access should be an overlay, not a replacement.

### 2. Use Traefik as the shared domain front door

For any instance with a domain:

- Cloudflare DNS points the hostname to the server IP
- Traefik listens on `80/443`
- Traefik routes `Host(instance.domain)` to `127.0.0.1:instance.http_port`
- The original service still listens on its assigned port

This gives one host two access paths:

- direct: `http://SERVER_IP:PORT`
- domain: `https://instance.example.com`

### 3. Use Cloudflare as the DNS control plane

Cloudflare should become the primary managed DNS provider for the shared DafeApp platform domain.

Recommended first rollout:

- exact per-instance DNS records
- one system-level Cloudflare token / zone for the platform domain
- optional Cloudflare proxy enabled for public domains

Custom domains from third-party providers should be supported without requiring DafeApp to own or manage that provider account. Users can point an `A` record manually and let DafeApp validate routing + SSL after that.

Recommended later rollout:

- wildcard records for large fleets
- Traefik DNS challenge if wildcard certificate issuance becomes necessary

### 4. Standardize on Traefik for long-term HTTPS routing

The repo currently has two HTTPS stories:

- Docker: Traefik + Let's Encrypt
- Bare-metal domain mode: nginx + certbot

The long-term design should converge on Traefik for both.

That does not require Docker for bare-metal Odoo itself. Traefik can run independently as a lightweight reverse proxy while Odoo remains a systemd service.

## Why this is the best fit for DafeApp

- It keeps the current successful provisioning path intact.
- It avoids forcing users away from direct `IP:PORT`, which is useful for bootstrap, debugging, and fallback.
- It reuses concepts the project already has in Docker mode.
- It gives the `dns/` app a real product role.
- It avoids multiplying reverse-proxy patterns over time.

## Recommended Rollout

### Phase 1. Build the DNS foundation in `dns/`

Add first-class models and services for:

- DNS provider account
- DNS zone
- DNS record
- domain assignment metadata for instances

Cloudflare should be the first provider implemented.

Store API tokens encrypted and scope them as tightly as possible.

### Phase 2. Add server-level domain routing settings

Extend server configuration so a server can opt into managed domain routing.

Suggested capabilities:

- base domain or selected DNS zone
- whether DafeApp manages DNS automatically
- whether Traefik-based HTTPS is enabled for that server

This should not introduce a new deployment mode. It should remain:

- `BARE_METAL`
- `DOCKER`

with optional managed domain routing layered on top.

### Phase 3. Install Traefik on bare-metal servers

Add a bare-metal gateway setup path that:

- installs Traefik
- opens ports `80` and `443`
- creates a dynamic config directory such as `/etc/traefik/dynamic/`
- starts Traefik as a systemd service or managed container

This should be separate from the Odoo runtime so Odoo services can remain unchanged.

### Phase 4. Update the instance create UX

Bare-metal instance creation should support both:

- a port
- an optional domain

The UI should stop treating domain as Docker-only.

For bare-metal:

- if no domain is provided, keep the current direct flow
- if a domain is provided, create the instance exactly as today, then add DNS and Traefik routing on top

### Phase 5. Move bare-metal domain routing from nginx to Traefik

Replace the long-term bare-metal domain path with Traefik route generation.

For each domain-backed instance:

- create or update a Traefik dynamic route
- point it at `127.0.0.1:instance.http_port`
- support websocket / realtime paths as needed

On instance delete or archive:

- remove the Traefik route
- remove or disable the DNS record

### Phase 6. Improve access URL modeling

The app should stop treating access as a single URL for all cases.

Recommended serializer/UI fields:

- `direct_access_url`
- `domain_access_url`
- `preferred_access_url`

Rules:

- if a domain exists and is healthy, prefer `https://domain`
- otherwise show `http://IP:PORT`

### Phase 7. Add reconciliation and monitoring

Add periodic jobs so the system repairs drift:

- if server IP changes, update Cloudflare DNS records
- if a Traefik route file is missing, regenerate it
- if a domain exists but SSL is not ready, surface that status clearly

Existing SSH reachability checks should remain the server connectivity source of truth.
Existing instance health checks can continue probing direct `IP:PORT` even when domains are enabled.

### Phase 8. Migrate safely

Do not flip existing installations all at once.

Recommended rollout order:

1. one zone
2. one server
3. one bare-metal instance with both direct and domain access
4. wider enablement for new servers
5. optional migration tools for older instances

## Bare-Metal Strategy: Recommended Details

### First release

Use:

- exact Cloudflare DNS records per instance
- Traefik HTTP challenge
- direct `IP:PORT` retained for health checks and fallback

Why this first:

- simplest operationally
- matches the current Traefik usage pattern already in the repo
- avoids putting Cloudflare DNS challenge credentials on every server

### Later release

Consider:

- wildcard DNS records
- Traefik DNS challenge
- wildcard certificate strategy

This becomes attractive once many instances share one base domain.

## Data Model Direction

The exact schema can evolve, but the system will likely need:

### In `dns/`

- `DnsProviderAccount`
- `DnsZone`
- `DnsRecord`
- `DomainAssignment` or equivalent binding to `OdooInstance`

### In `deployments/`

Potential additions on `OdooServer`:

- `managed_dns_enabled`
- `managed_dns_zone`
- `domain_routing_enabled`
- `tls_mode`

Potential additions on `OdooInstance`:

- `domain_status`
- `domain_last_checked_at`
- `ssl_status`
- `ssl_error`
- `direct_access_url` and `preferred_access_url` as serializer-level properties

## Important Technical Notes

### Websocket / realtime support

Docker mode already routes websocket traffic separately.

Bare-metal Traefik routing must account for the same Odoo realtime path behavior, not just plain HTTP proxying.

### Proxy headers

Any domain-fronted bare-metal deployment should use proxy-aware Odoo settings so scheme and host are interpreted correctly behind Traefik.

### Cloudflare SSL mode

If Cloudflare proxying is enabled, the intended production mode should be `Full (strict)` once origin TLS is fully configured.

### Health checks

Keep instance health checks against direct `IP:PORT` initially. That gives a stable signal even if DNS or Cloudflare configuration is temporarily broken.

## Risks and Tradeoffs

### If we keep nginx for bare-metal domains

- fewer changes short-term
- but DafeApp keeps two reverse-proxy systems permanently
- higher maintenance cost over time

### If we move too quickly to wildcard DNS

- stronger scaling story
- but more moving parts
- higher risk before the `dns/` app is mature

### If we remove direct `IP:PORT`

- cleaner public story
- but worse operability for debugging and recovery

Direct access should stay.

## Success Criteria

The implementation is successful when:

1. A bare-metal instance can be created with only `IP:PORT`.
2. A bare-metal instance can also be created with both `IP:PORT` and `https://domain`.
3. DafeApp creates and removes Cloudflare DNS records automatically.
4. Traefik routes domain traffic to the correct instance port.
5. SSL is provisioned and renewed without per-instance manual work.
6. The UI clearly shows DNS state, SSL state, and both access paths.

## Recommendation Summary

The best path for this project is:

- keep the existing `IP:PORT` logic
- add optional domain routing on top
- use Cloudflare as the managed DNS provider
- standardize on Traefik as the long-term HTTPS layer
- implement exact-record automation first
- add wildcard and DNS-challenge features later
