# Plan: Push-Based Heartbeat Agent for Server Connectivity

## Context
Currently DafeApp checks server connectivity by SSHing into every active server every 180 seconds
from the Celery worker (one big task, all servers sequentially). This is resource-heavy — each SSH
handshake takes seconds, and with many servers it blocks the worker. The SSH key is also not
authorized on most PYOS servers, causing authentication failures in logs.

**New approach:** Flip to push-based. During provisioning, install a lightweight `dafeapp-heartbeat`
systemd service on each server. It POSTs to DafeApp every 60 seconds. If DafeApp receives no
heartbeat for 5 minutes → mark disconnected. The SSH polling task is kept as fallback for
legacy servers (no agent installed).

---

## Files to Create / Modify

1. `deployments/models.py` — add `agent_token`, `last_heartbeat_at`, `last_agent_repair_at` fields
2. `deployments/migrations/XXXX_heartbeat_fields.py` — new migration
3. `deployments/views.py` — new `HeartbeatView` endpoint
4. `deployments/urls.py` — register heartbeat URL (unauthenticated)
5. `deployments/tasks.py` — heartbeat timeout task, skip SSH poll for agents, and stale-agent repair task
6. `dafeapp/settings.py` — add beat schedule entry
7. `deployments/signals.py` — sync the DB-backed celery beat tasks
8. `infra/ansible/setup_odoo_server_bare.yml` — install heartbeat agent at end
9. `infra/ansible/setup_docker_host.yml` — install heartbeat agent at end
10. `infra/ansible/install_dafeapp_heartbeat_agent.yml` — shared repair/install playbook

---

## Step 1 — `deployments/models.py`: Add heartbeat fields to `OdooServer`

After `last_checked_at` (line 311), add:
```python
import uuid  # already imported or add at top

agent_token = models.UUIDField(
    default=uuid.uuid4,
    unique=True,
    db_index=True,
    editable=False,
)
last_heartbeat_at = models.DateTimeField(null=True, blank=True)
last_agent_repair_at = models.DateTimeField(null=True, blank=True)
```

`agent_token` is auto-generated on server creation. It's the shared secret the agent uses
to identify itself — no user auth needed on the heartbeat endpoint.

---

## Step 2 — Migration

```bash
python manage.py makemigrations deployments --name heartbeat_fields
python manage.py migrate
```

---

## Step 3 — `deployments/views.py`: New `HeartbeatView`

Add near the other lightweight API views. This endpoint is intentionally unauthenticated
(the token IS the auth). It must be fast — no session middleware, no org lookup.

```python
from django.views import View
from django.utils import timezone

class HeartbeatView(View):
    """Called by the dafeapp-heartbeat systemd service on each managed server."""

    def post(self, request, token):
        from deployments.models import OdooServer
        try:
            server = OdooServer.objects.only(
                "pk", "is_active", "is_reachable", "last_heartbeat_at", "last_checked_at"
            ).get(agent_token=token, is_active=True)
        except OdooServer.DoesNotExist:
            return JsonResponse({"ok": False}, status=404)

        now = timezone.now()
        server.last_heartbeat_at = now
        server.last_checked_at = now
        server.is_reachable = True
        server.save(update_fields=["last_heartbeat_at", "last_checked_at", "is_reachable", "updated_at"])
        return JsonResponse({"ok": True})
```

---

## Step 4 — `deployments/urls.py`: Register heartbeat URL

Find the API urlpatterns list and add (no login_required):
```python
path("odoo/servers/heartbeat/<uuid:token>/", views.HeartbeatView.as_view(), name="server-heartbeat"),
```

This goes in the `api/deployments/` URL namespace.

---

## Step 5 — `deployments/tasks.py`: Timeout, skip logic, and stale-agent repair

**5a. New `mark_disconnected_servers` task** — add near `check_server_connectivity`:

```python
@shared_task
def mark_disconnected_servers():
    """Mark agent-enabled servers disconnected if no heartbeat for 5 minutes."""
    threshold = timezone.now() - timedelta(minutes=5)
    updated = OdooServer.objects.filter(
        is_active=True,
        last_heartbeat_at__isnull=False,   # only servers with agent installed
        last_heartbeat_at__lt=threshold,
        is_reachable=True,
    ).update(is_reachable=False, last_checked_at=timezone.now())
    if updated:
        logger.info("Heartbeat timeout: marked %d server(s) as disconnected.", updated)
```

**5b. Skip SSH polling for agent-enabled servers** in `check_server_connectivity` — inside the
server loop (around line 3789), add this guard before the SSH probe:

```python
# Skip servers using push-based heartbeat agent
if server.last_heartbeat_at is not None:
    continue
```

This preserves backward compatibility — legacy servers (no agent) still get SSH-polled.

**5c. Pass `agent_token` to Ansible** in `_configure_odoo_server_inner` — find where `extra_vars`
is built (around line 2890) and add:

```python
extra_vars["agent_token"] = str(server.agent_token)
extra_vars["dafeapp_heartbeat_url"] = (
    getattr(settings, "SITE_URL", "").rstrip("/")
    + f"/api/deployments/odoo/servers/heartbeat/{server.agent_token}/"
)
```

Do the same in `_configure_docker_host_inner` (around line 5271) — pass the same two vars.

**5d. Add `repair_stale_heartbeat_agents`** — if a server has not sent a heartbeat for 20 minutes,
try SSH once per hour. If SSH works, reinstall/restart the heartbeat service so it recovers even if
someone manually stopped or removed it.

---

## Step 6 — `dafeapp/settings.py`: Add beat schedule

In `CELERY_BEAT_SCHEDULE` dict, add:
```python
"mark-disconnected-servers": {
    "task": "deployments.tasks.mark_disconnected_servers",
    "schedule": 60.0,   # run every 60 seconds
},
"repair-stale-heartbeat-agents": {
    "task": "deployments.tasks.repair_stale_heartbeat_agents",
    "schedule": 3600.0,   # run every hour
},
```

Because this project uses `django_celery_beat` database scheduling, the same tasks should also be
synced in `deployments/signals.py`.

---

## Step 7 — Ansible: Install heartbeat agent on bare-metal servers

In `infra/ansible/setup_odoo_server_bare.yml`, add these tasks **after** the UFW step (line ~197):

```yaml
- name: Create DafeApp agent directory
  file:
    path: /opt/dafeapp-agent
    state: directory
    mode: "0755"

- name: Install heartbeat script
  copy:
    dest: /opt/dafeapp-agent/heartbeat.sh
    mode: "0755"
    content: |
      #!/bin/bash
      DAFEAPP_URL="{{ dafeapp_heartbeat_url }}"
      while true; do
        curl -s -X POST "$DAFEAPP_URL" --max-time 10 --silent --output /dev/null || true
        sleep 60
      done

- name: Install heartbeat systemd service
  copy:
    dest: /etc/systemd/system/dafeapp-heartbeat.service
    content: |
      [Unit]
      Description=DafeApp Heartbeat Agent
      After=network-online.target
      Wants=network-online.target

      [Service]
      Type=simple
      ExecStart=/opt/dafeapp-agent/heartbeat.sh
      Restart=always
      RestartSec=30

      [Install]
      WantedBy=multi-user.target

- name: Enable and start heartbeat service
  systemd:
    name: dafeapp-heartbeat
    enabled: true
    state: started
    daemon_reload: true
```

---

## Step 8 — Ansible: Install heartbeat agent on Docker hosts

In `infra/ansible/setup_docker_host.yml`, add the **exact same 4 tasks** after the UFW step
(line ~195). The Docker host is a Linux server too — the systemd service runs on the host,
not inside a container. This correctly reports host-level liveness.

---

## Architecture Summary

```
Server (systemd)          DafeApp (Django)
─────────────────         ─────────────────────────────────────
dafeapp-heartbeat.sh  →   POST /api/deployments/odoo/servers/heartbeat/<uuid>/
  every 60s                 → sets is_reachable=True, last_heartbeat_at=now

Celery beat (60s)         mark_disconnected_servers task
                            → if last_heartbeat_at < now-5min → is_reachable=False

Celery beat (180s)        check_server_connectivity (unchanged)
                            → skips servers where last_heartbeat_at is not None
                            → still polls legacy/PYOS servers via SSH

Celery beat (3600s)       repair_stale_heartbeat_agents
                            → if heartbeat missing for 20+ min, try SSH once/hour
                            → if SSH works, reinstall/restart heartbeat service
```

---

## Backward Compatibility

- Servers provisioned **before** this change: `last_heartbeat_at=None`, `agent_token` exists
  but agent not installed → SSH polling continues unchanged for them
- Servers provisioned **after** this change: agent installed → heartbeat path, no SSH polling
- PYOS/ExternalServer: unchanged (still SSH-polled, heartbeat not installed on user-owned VPS)

---

## Verification

1. Run `python manage.py migrate` — confirm new fields appear on `OdooServer`
2. Provision a new test server — confirm Ansible installs `dafeapp-heartbeat.service`
3. On server: `systemctl status dafeapp-heartbeat` → should show active/running
4. Watch DafeApp logs: should see POST hits on `/api/deployments/odoo/servers/heartbeat/<token>/`
5. After first heartbeat: server badge → "Connected" (green)
6. Stop heartbeat service on server: `systemctl stop dafeapp-heartbeat`
7. Wait 5+ minutes → server badge → "Disconnected" (red)
8. Restart service → badge returns to "Connected" within 60 seconds
9. Confirm Celery worker logs no longer show SSH auth failures for agent-enabled servers


## Recovery Addendum

For heartbeat-enabled servers, DafeApp should not keep SSH-polling every few minutes. If the
heartbeat stops for 5 minutes, mark the server disconnected. If it remains stale for 20 minutes,
try SSH once per hour; when SSH succeeds, reinstall/restart the heartbeat service so it comes back
even if it was manually stopped or deleted.
