# Dbug Commands

This file collects the most useful debug commands for DafeApp server provisioning, Odoo instance creation, DNS, Traefik, and Celery troubleshooting.

---

## App Processes

Start the local app processes:

```bash
# Django / Daphne
daphne -b 0.0.0.0 -p 8000 dafeapp.asgi:application

# Celery worker
celery -A dafeapp worker -l info

# Celery beat
celery -A dafeapp beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
```

Restart Celery after code or `.env` changes:

```bash
pkill -f "celery -A dafeapp"
celery -A dafeapp worker -l info
celery -A dafeapp beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
```

View Docker logs for Django and Celery:

```bash
# Local development
docker compose logs -f web
docker compose logs -f celery_worker
docker compose logs -f celery_beat

# Production on the droplet
cd /opt/dafeapp
docker compose -f docker-compose.prod.yml logs -f web
docker compose -f docker-compose.prod.yml logs -f celery_worker
docker compose -f docker-compose.prod.yml logs -f celery_beat
```

Show recent logs without following:

```bash
docker compose -f docker-compose.prod.yml logs --tail=100 web
docker compose -f docker-compose.prod.yml logs --tail=200 celery_worker
docker compose -f docker-compose.prod.yml logs --tail=200 celery_beat
```

Check container state before reading logs:

```bash
docker compose -f docker-compose.prod.yml ps
```

---

## Django Shell Checks

Inspect one instance:

```bash
/home/rock/Desktop/2026_Projects/my/lvenv/bin/python manage.py shell -c "from deployments.models import OdooInstance; i=OdooInstance.objects.get(pk=30); print('domain=', i.domain); print('domain_status=', i.domain_status); print('ssl_status=', i.ssl_status); print(i.provisioning_log)"
```

Inspect one instance by database name:

```bash
/home/rock/Desktop/2026_Projects/my/lvenv/bin/python manage.py shell -c "from deployments.models import OdooInstance; i=OdooInstance.objects.get(db_name='cheropdb'); print('domain=', i.domain); print('domain_status=', i.domain_status); print('ssl_status=', i.ssl_status); print(i.provisioning_log)"
```

Inspect domain assignments for an instance:

```bash
/home/rock/Desktop/2026_Projects/my/lvenv/bin/python manage.py shell -c "from deployments.models import OdooInstance; from dns.models import DomainAssignment; i=OdooInstance.objects.get(db_name='cheropdb'); print(list(DomainAssignment.objects.filter(instance=i).values('domain','status','is_managed','provider_record_id','last_error')))"
```

Inspect one Odoo server log:

```bash
/home/rock/Desktop/2026_Projects/my/lvenv/bin/python manage.py shell -c "from deployments.models import OdooServer; s=OdooServer.objects.get(ip_address='64.227.183.213'); print(s.provisioning_log)"
```

Inspect one external server SSH configuration:

```bash
/home/rock/Desktop/2026_Projects/my/lvenv/bin/python manage.py shell -c "from cloud.models import ExternalServer; s=ExternalServer.objects.get(pk=21); print('auth_type=', s.auth_type); print('ssh_key_path=', repr(s.ssh_key_path))"
```

Run Django checks:

```bash
/home/rock/Desktop/2026_Projects/my/lvenv/bin/python manage.py check
```

---

## DNS Checks

Check a hostname against the local resolver:

```bash
dig cherop.dafeapp.com
nslookup cherop.dafeapp.com
```

Check authoritative/public DNS directly:

```bash
dig cherop.dafeapp.com @1.1.1.1
dig cherop.dafeapp.com @8.8.8.8
```

Check nameserver delegation for the root domain:

```bash
dig NS dafeapp.com +short
```

Trace where DNS breaks:

```bash
dig +trace cherop.dafeapp.com
```

Expected outcome when DNS is healthy:

```text
cherop.dafeapp.com -> 64.227.183.213
```

---

## Cloudflare Checks

Things to verify in the Cloudflare dashboard:

- Zone status is active.
- The DNS record exists, for example:
  - `Type`: `A`
  - `Name`: `cherop`
  - `Content`: `64.227.183.213`
- The registrar is actually using Cloudflare nameservers.

For DafeApp platform DNS automation, these `.env` values must be set:

```env
PLATFORM_BASE_DOMAIN=dafeapp.com
PLATFORM_DNS_PROVIDER=CLOUDFLARE
PLATFORM_DNS_API_TOKEN=...
PLATFORM_DNS_ZONE_ID=...
PLATFORM_DNS_PROXIED=False
```

---

## Traefik Checks On Target Server

Check whether Traefik is installed and running:

```bash
sudo systemctl status traefik --no-pager
```

Check if ports 80 and 443 are listening:

```bash
sudo ss -tulpn | grep -E ':80|:443'
```

List Traefik dynamic route files:

```bash
sudo ls -l /etc/traefik/dynamic/
```

Inspect one route file:

```bash
sudo cat /etc/traefik/dynamic/dafeapp-cherop-dafeapp-com.yml
```

Inspect Traefik static config:

```bash
sudo cat /etc/traefik/traefik.yml
```

Expected route behavior:

- multiple subdomains point to the same server IP in DNS
- Traefik uses the hostname to route each subdomain to the correct backend port

---

## Install Traefik Manually

Install and start Traefik on a bare-metal Odoo server:

```bash
ansible-playbook -i '64.227.183.213,' /home/rock/Desktop/2026_Projects/my/dafeapp/infra/ansible/setup_bare_traefik_gateway.yml \
  --private-key /home/rock/.ssh/dafeapp_id_ed25519 \
  -u root \
  -e "traefik_dynamic_dir=/etc/traefik/dynamic" \
  -e "traefik_tls_mode=LETS_ENCRYPT" \
  -e "traefik_acme_email=you@dafeapp.com" \
  -e "traefik_acme_storage=/var/lib/traefik/acme.json" \
  -e "traefik_log_level=INFO" \
  -e "traefik_version=3.1.2"
```

Apply one Traefik route manually:

```bash
ansible-playbook -i '64.227.183.213,' /home/rock/Desktop/2026_Projects/my/dafeapp/infra/ansible/apply_bare_traefik_route.yml \
  --private-key /home/rock/.ssh/dafeapp_id_ed25519 \
  -u root \
  -e "domain=cherop.dafeapp.com" \
  -e "http_port=8077" \
  -e "route_name=dafeapp-cherop-dafeapp-com" \
  -e "route_file=/etc/traefik/dynamic/dafeapp-cherop-dafeapp-com.yml" \
  -e "traefik_dynamic_dir=/etc/traefik/dynamic" \
  -e "traefik_tls_mode=LETS_ENCRYPT"
```

Delete one Traefik route manually:

```bash
ansible-playbook -i '64.227.183.213,' /home/rock/Desktop/2026_Projects/my/dafeapp/infra/ansible/delete_bare_traefik_route.yml \
  --private-key /home/rock/.ssh/dafeapp_id_ed25519 \
  -u root \
  -e "domain=cherop.dafeapp.com" \
  -e "route_name=dafeapp-cherop-dafeapp-com" \
  -e "route_file=/etc/traefik/dynamic/dafeapp-cherop-dafeapp-com.yml" \
  -e "traefik_dynamic_dir=/etc/traefik/dynamic"
```

---

## Odoo Runtime Logs On Target Server

Check which process owns a port:

```bash
sudo ss -tulpn | grep 8072
sudo lsof -i :8072
```

Check one Odoo systemd service:

```bash
sudo systemctl status odoo-cheropdb --no-pager
```

Check runtime logs:

```bash
journalctl -u odoo-cheropdb -n 120 --no-pager -o short-iso
```

Check for duplicate Odoo processes:

```bash
ps -ef | grep odoo | grep 8072
```

Stop stale processes if needed:

```bash
sudo systemctl stop odoo-cheropdb
sudo pkill -f "cheropdb"
sudo systemctl start odoo-cheropdb
```

Common failure:

```text
OSError: [Errno 98] Address already in use: ('0.0.0.0', 8072)
```

This means another process is already bound to that port.

---

## SSH / PYOS Checks

Recommended private key path:

```text
/home/rock/.ssh/dafeapp_id_ed25519
```

Common mistake:

- saving public key text like `ssh-ed25519 AAAA...` into `ssh_key_path`
- `ssh_key_path` must be a local private key file path, not pasted key contents

Check the local private key file:

```bash
ls -l /home/rock/.ssh/dafeapp_id_ed25519
```

Check SSH access to the target server:

```bash
ssh -i /home/rock/.ssh/dafeapp_id_ed25519 root@64.227.183.213
```

If DafeApp logs show:

```text
SSH key path looks like a public key string, not a file path.
```

then update the saved server config to use:

```text
/home/rock/.ssh/dafeapp_id_ed25519
```

---

## Repo / Code Checks

Verify the Traefik gateway playbook exists:

```bash
ls -l /home/rock/Desktop/2026_Projects/my/dafeapp/infra/ansible/setup_bare_traefik_gateway.yml
```

Verify the code path points to it:

```bash
grep -n "setup_bare_traefik_gateway" deployments/tasks.py
```

Compile-check updated Python files:

```bash
/home/rock/Desktop/2026_Projects/my/lvenv/bin/python -m py_compile deployments/tasks.py
/home/rock/Desktop/2026_Projects/my/lvenv/bin/python -m py_compile deployments/views.py
/home/rock/Desktop/2026_Projects/my/lvenv/bin/python -m py_compile cloud/pyos.py
```

---

## GitHub Webhook — Git Repo Auto-Update

### How It Works

GitHub sends ALL push events to `POST /api/deployments/github/webhook/`.
Django decides what to act on — GitHub does no filtering.

Flow for each push event:

1. Validate HMAC-SHA256 signature (`X-Hub-Signature-256`) against `GITHUB_WEBHOOK_SECRET`
2. Only process `push` events (ping and others are logged as IGNORED)
3. Extract `repository.full_name` and branch from `ref` (`refs/heads/develop` → `develop`)
4. Query all `OdooInstanceGitRepo` records where:
   - `auto_update=True`
   - `is_enabled=True`
   - `instance.status=RUNNING`
   - Not currently `CLONING`
   - No `pinned_commit`
   - Git URL resolves to the same GitHub full_name
   - `branch` matches exactly
5. For each matched repo → create `DeploymentJob` → dispatch `update_instance_repo` Celery task
6. Record a `GitHubWebhookEvent` log entry

### Webhook Auto-Registration

When a repo is saved with `auto_update=True` and a GitHub OAuth or token credential,
`_ensure_github_push_webhook()` calls the GitHub API to create/verify the push webhook automatically.
No manual setup on GitHub required.

### Key Fields on OdooInstanceGitRepo

| Field | Purpose |
| --- | --- |
| `branch` | Which branch to listen to |
| `auto_update` | Enable/disable webhook-triggered updates |
| `pinned_commit` | If set, webhook is ignored for this repo |
| `last_pulled_commit` | SHA of the last successfully pulled commit |
| `last_pulled_at` | Timestamp of last successful pull |
| `status` | CONNECTED / UPDATING / ERROR / CLONING / DISCONNECTED |
| `last_sync_log` | Output log of the last git operation |

### GitHubWebhookEvent Model

Every incoming webhook is logged in `GitHubWebhookEvent`:

| Field | Description |
| --- | --- |
| `repository` | GitHub full_name (owner/repo) |
| `branch` | Branch that was pushed |
| `head_commit_sha` | Commit SHA from the push payload |
| `head_commit_message` | Commit message (truncated to 500 chars) |
| `pusher_name` | GitHub username who pushed |
| `status` | PROCESSED / IGNORED / ERROR |
| `ignore_reason` | Why it was ignored (e.g. "ping", "branch mismatch") |
| `matched_repo_ids` | OdooInstanceGitRepo IDs that matched |
| `queued_repo_ids` | OdooInstanceGitRepo IDs that were queued for update |
| `received_at` | Timestamp |

### Debug Commands

Check recent webhook events via API:

```bash
curl -s http://localhost:8000/api/deployments/github/webhook-events/ \
  -H "Cookie: sessionid=..." | python3 -m json.tool
```

Filter by repo/branch:

```bash
curl -s "http://localhost:8000/api/deployments/github/webhook-events/?repository=myorg/myrepo&branch=develop"
```

Check in Django admin: `/admin/deployments/githubwebhookevent/`

Simulate a push webhook locally (no signature, requires `GITHUB_WEBHOOK_SECRET` unset):

```bash
curl -X POST http://localhost:8000/api/deployments/github/webhook/ \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: push" \
  -d '{"ref":"refs/heads/develop","repository":{"full_name":"myorg/myrepo"},"head_commit":{"id":"abc123","message":"test"},"pusher":{"name":"dev"}}'
```

Trigger manual repo update (bypass webhook):

```bash
# Via API — sync endpoint
curl -X POST http://localhost:8000/api/deployments/odoo/instances/<id>/repos/<repo_id>/sync/ \
  -H "Cookie: sessionid=..."
```

Check Celery task for repo update:

```bash
celery -A dafeapp inspect active
```

### Required Environment Variable

```bash
GITHUB_WEBHOOK_SECRET=your-secret-here   # Must match what you set on GitHub webhook
```

If `GITHUB_WEBHOOK_SECRET` is empty, signature validation is skipped (dev only).

---

## Quick Triage Order

When a domain does not work, use this order:

1. Check Cloudflare DNS record exists.
2. Check `dig` returns the server IP.
3. Check `dig NS dafeapp.com +short` returns Cloudflare nameservers.
4. Check `sudo systemctl status traefik`.
5. Check `sudo ls -l /etc/traefik/dynamic/`.
6. Check the instance `provisioning_log`.
7. Check the Odoo port and service on the target server.

When instance creation fails, use this order:

1. Check Celery worker logs.
2. Check instance `provisioning_log`.
3. Check Odoo systemd logs on the target server.
4. Check remote port conflicts with `ss` / `lsof`.
5. Check SSH reachability and saved key path.



docker compose -f docker-compose.prod.yml up -d --force-recreate web celery_worker celery_beat

docker compose -f docker-compose.prod.yml up -d --force-recreate web celery_worker celery_beat caddy


docker compose -f docker-compose.prod.yml exec web sh -lc 'env | grep -E "PLATFORM_|TRAEFIK_|ODOO_ADMIN_EMAIL"'
docker compose -f docker-compose.prod.yml exec celery_worker sh -lc 'env | grep -E "PLATFORM_|TRAEFIK_|ODOO_ADMIN_EMAIL"'
