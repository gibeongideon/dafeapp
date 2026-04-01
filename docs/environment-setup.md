# Environment Setup

## Prerequisites

- Python 3.13
- PostgreSQL 16
- Redis 7
- Terraform (for managed cloud provisioning)
- Ansible (for server configuration)
- SSH key access to target servers

---

## Local Development

### 1. Clone and create virtualenv

```bash
python3.13 -m venv lvenv
source lvenv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in values:

```bash
cp .env.example .env
```

**Required variables:**

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | Django secret key |
| `DATABASE_URL` | `postgres://user:pass@host:5432/dbname` |
| `REDIS_URL` | `redis://localhost:6379/0` |
| `FIELD_ENCRYPTION_KEY` | Fernet key (generate with `cryptography`) |

**Generate a Fernet key:**
```python
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
```

**Infrastructure variables (used by deployments, but some now have repo-local defaults):**

| Variable | Description |
|----------|-------------|
| `ANSIBLE_ODOO_SERVER_PLAYBOOK` | `infra/ansible/setup_odoo_server_bare.yml` (default if unset) |
| `ANSIBLE_ODOO_INSTANCE_PLAYBOOK` | `create_odoo_instance.yml` |
| `ANSIBLE_ODOO_INSTANCE_DIRECT_PLAYBOOK` | `create_odoo_instance_direct.yml` |
| `ANSIBLE_ODOO_INSTANCE_DELETE_PLAYBOOK` | `delete_odoo_instance_direct.yml` |
| `TERRAFORM_SERVER_MODULE_DIR` | Absolute path to `infra/terraform/odoo_server/` |
| `ANSIBLE_SSH_KEY_PATH` | Path to DafeApp Ed25519 private key |
| `ANSIBLE_SSH_USER` | SSH user on target servers (default: `root`) |
| `ODOO_ADMIN_EMAIL` | Email for Certbot SSL (use placeholder to skip SSL) |

**Cloud provider variables (needed for managed provisioning):**

| Variable | Description |
|----------|-------------|
| `DIGITALOCEAN_TOKEN` | DO API token |
| `AWS_ACCESS_KEY_ID` | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | AWS secret |
| `AWS_DEFAULT_REGION` | e.g., `us-east-1` |
| `DNS_PROVIDER` | `digitalocean` or `route53` |
| `DNS_ROOT_DOMAIN` | Base domain for instances |
| `AWS_ROUTE53_ZONE_ID` | Route53 hosted zone ID |

### 3. Initialize database

```bash
python manage.py migrate
python manage.py createsuperuser
```

### 4. Run services

```bash
# Terminal 1 — Django/Daphne
daphne -b 0.0.0.0 -p 8000 dafeapp.asgi:application

# Terminal 2 — Celery worker
celery -A dafeapp worker -l info

# Terminal 3 — Celery Beat (scheduler)
celery -A dafeapp beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
```

---

## Docker Compose (Development)

```bash
docker-compose up --build
```

Services started:
- `db` — PostgreSQL 16 on port 5432
- `redis` — Redis 7 on port 6379
- `web` — Daphne on port 8000
- `celery_worker` — Celery worker
- `celery_beat` — Celery Beat scheduler

---

## OAuth Setup (optional)

1. Create OAuth apps at Google, GitHub, or GitLab developer consoles.
2. Add callback URL: `http://localhost:8000/accounts/<provider>/login/callback/`
3. Add credentials to `.env`:
   ```
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   GITHUB_CLIENT_ID=...
   GITHUB_CLIENT_SECRET=...
   ```
4. Configure in Django admin under **Social Applications**.




COMMANDS

celery -A dafeapp worker -l info
celery -A dafeapp beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
