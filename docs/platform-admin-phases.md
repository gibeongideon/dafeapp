# Platform Admin Phases

## Phase 1: Foundation

- Replace the default Django admin site with a custom platform admin site at `/admin/`.
- Allow internal platform admins to authenticate into the admin even when they are not traditional Django staff users.
- Surface a first platform-wide dashboard with global counts, recent organizations, recent audit activity, and direct links into key admin models.
- Keep the existing model registrations and CRUD screens intact so the platform team can operate across every organization from one place.

## Phase 2: Cross-Org Navigation

- Add organization drill-down shortcuts from the platform dashboard.
- Add scoped filters and saved list presets for common platform workflows.
- Add a safe "view as organization" flow for inspecting the customer-facing dashboard without changing memberships.

## Phase 3: Operations Console

- Add dashboard sections for failed deployment jobs, unhealthy instances, stale verifications, and subscription risk.
- Add platform-only recovery actions where appropriate, such as retrying jobs or re-running verification flows.
- Register remaining operational models that are useful to support and SRE workflows.

## Phase 4: Audit And Safety

- Expand audit visibility with better filtering, event summaries, and links back to affected records.
- Lock down sensitive models to read-only where platform admins should inspect but not mutate.
- Add dedicated tests for platform-admin authentication, dashboard visibility, and admin permissions.

## Phase 5: Role Hardening

- Introduce finer-grained internal roles such as support, finance, and operations.
- Split full platform admin from read-only or domain-limited staff access.
- Add per-role admin index cards and model visibility rules.
