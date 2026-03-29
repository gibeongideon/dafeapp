# Deployment Flow

This document describes how DafeApp provisions an Odoo server and creates Odoo instances.

---

## 1. Add Infrastructure

Before deploying, the user connects their infrastructure.

### Option A — PYOS (External SSH Server)
1. User goes to **Deployments > Create Server**, picks **PYOS**, and fills a single form with server name, host, SSH port, username, auth method, Odoo version, and deployment mode.
2. DafeApp stores the external server details, creates the infrastructure wrapper, and queues provisioning in one submission.
3. DafeApp verifies SSH access and then prepares the Odoo environment for later instance creation.

### Option B — Managed Cloud (DigitalOcean / AWS)
1. User goes to **Cloud > Add Cloud Account**, enters API token / AWS keys.
2. DafeApp verifies credentials by calling the provider API.
3. Account is stored encrypted in the database.

---

## 2. Create an Infrastructure Record

In **Deployments > Infrastructure**, the user links an org to either a PYOS server or a cloud account. This creates an `Infrastructure` record (`type = PYOS | MANAGED`).

---

## 3. Provision an Odoo Server Environment

The user fills in:
- PYOS: server name, host, SSH port, username, auth method, Odoo version, and deployment mode
- Managed: server name, region, size, Odoo version, deployment mode, optional domain

A Celery task `provision_odoo_server` is triggered:

```
provision_odoo_server(odoo_server_id)
    │
    ├── If PYOS:
    │       Skip Terraform
    │       Use ExternalServer IP directly
    │
    └── If MANAGED:
            Write vars.json (provider, name, version, region, size, org)
            Run: terraform init + terraform apply
            Parse output → get public_ip
            Create Instance record
    │
    └── Both paths → configure_odoo_server(odoo_server_id)
                        Run Ansible: setup_odoo_server_bare.yml
                        extra_vars: odoo_version, server_name, dns_domain,
                                    website_name, admin_email
                        Uploads version-specific odoo_install.sh from scripts/installscript/<ver>, patches vars,
                        prepares the Odoo environment only (no standalone service)
```

**OdooServer status progression:**
`PENDING` → `PROVISIONING` → `CONFIGURING` → `PROVISIONED` (or `FAILED`)

### Testing the same flow locally or over SSH

The bare-metal bootstrap used by the UI is the same one you can run from the CLI:

- Local simulation: `sudo ./scripts/deploy_bare.sh --local --fresh --version 19`
- SSH simulation: `./scripts/deploy_bare.sh --ip <server-ip> --user <ssh-user> --key ~/.ssh/<private-key> --version 19`
- Legacy standalone mode: add `--standalone` if you explicitly want a running Odoo service on 8069.

Local mode is for phase-one validation on your workstation or a disposable VM. SSH mode is the production-like path and is what the DafeApp UI drives for PYOS / bare-metal servers. By default, both paths only prepare the environment for later instance creation.

### Simulating instance creation with Ansible

After the server environment is provisioned, you can simulate instance creation by running the instance playbook directly against the host.

For bare-metal / direct-IP instances:

```bash
ansible-playbook infra/ansible/create_odoo_instance_direct.yml \
  -i 168.144.24.219, \
  --user root \
  --private-key ~/.ssh/id_ed25519 \
  --extra-vars "odoo_version=19 db_name=app1 instance_name=app1 http_port=8070"
```

For a local VM or workstation simulation:

```bash
sudo ansible-playbook infra/ansible/create_odoo_instance_direct.yml \
  -i localhost, -c local \
  --extra-vars "odoo_version=19 db_name=app1 instance_name=app1 http_port=8070"
```

If your local `sudo` requires a password, add `-K` instead of `sudo` and let Ansible prompt for become access.

For domain-based instances with nginx and SSL:

```bash
ansible-playbook infra/ansible/create_odoo_instance.yml \
  -i 168.144.24.219, \
  --user root \
  --private-key ~/.ssh/id_ed25519 \
  --extra-vars "odoo_version=19 db_name=app1 instance_name=app1 domain=app1.example.com http_port=8070 letsencrypt_email=you@example.com"
```

The app uses the same playbooks from `deployments/tasks.py`, so these commands are the closest manual simulation of what DafeApp runs after server creation.

### DNS / SSL direction

Today, the project supports:

- direct bare-metal access via `IP:PORT`
- Docker domain routing via Traefik
- bare-metal domain routing via `nginx + certbot`

The intended next-step architecture is to keep direct `IP:PORT` access, but move long-term domain routing toward:

- Cloudflare-managed DNS
- Traefik for HTTPS termination and host-based routing
- first-class domain lifecycle management inside the `dns/` app

That plan is documented in [dns-ssl-implementation-plan.md](dns-ssl-implementation-plan.md).

The instance playbooks now include PostgreSQL preflight checks and bootstrap logic:

- `pg_lsclusters` is checked first so we can detect whether a real cluster exists.
- If no cluster is present, the playbook creates a default `main` cluster with `pg_createcluster`.
- If PostgreSQL is installed but the instance owner role is missing, the playbook creates it before database creation.
- `pg_isready` is still used as a readiness gate before `createdb`.

That means a failed instance run now points to a real PostgreSQL readiness or role problem instead of a generic socket error.

---

## 4. Create an Odoo Instance

After a server is `PROVISIONED`, the user can create instances on it.

A Celery task `create_odoo_instance` is triggered:

```
create_odoo_instance(instance_id)
    │
    ├── No domain → run: create_odoo_instance_direct.yml
    │       Creates: /odoo/<db_name>/odoo.conf
    │                systemd service: odoo-<db_name>.service
    │                Opens UFW port (http_port)
    │
    └── Domain → run: create_odoo_instance.yml
            Current path:
                Creates: nginx site + SSL (certbot)
                         /etc/odoo/<db_name>.conf
                         systemd service
            Planned path:
                Keep systemd + host port
                Add Cloudflare DNS + Traefik route on top
```

**OdooInstance status progression:**
`PENDING` → `CONFIGURING` → `RUNNING` (or `FAILED`)

---

## 5. Delete an Odoo Instance

Celery task `delete_odoo_instance`:
1. Stops and disables the systemd service.
2. Drops the PostgreSQL database.
3. Removes the config file.
4. Closes the UFW firewall port.
5. Marks instance as `DELETED`.

---

## 6. Connectivity Monitoring

A periodic Celery Beat task (`check_server_connectivity`) runs every **2 minutes**:
- Attempts SSH/TCP connection to all `OdooServer` records.
- Updates `is_reachable` and `last_checked_at` fields.

---

## Ansible Playbooks Reference

| Playbook | When used |
|----------|-----------|
| `setup_odoo_server_bare.yml` | Server provisioning (both PYOS and MANAGED) |
| `setup_odoo_server.yml` | Fallback: manual GitHub clone approach |
| `create_odoo_instance.yml` | Instance creation with domain + nginx + SSL |
| `create_odoo_instance_direct.yml` | Instance creation with direct IP:PORT (no nginx) |
| `delete_odoo_instance_direct.yml` | Instance deletion and cleanup |

For the planned Cloudflare + Traefik DNS/SSL rollout, see [dns-ssl-implementation-plan.md](dns-ssl-implementation-plan.md).

---

## Terraform Reference

**Module location:** `infra/terraform/odoo_server/`

**Supported providers:** DigitalOcean, AWS

**Input vars written by tasks.py:**
```json
{
  "provider": "DIGITALOCEAN | AWS",
  "name": "server-name",
  "odoo_version": "17 | 18 | 19",
  "region": "nyc3",
  "size": "s-2vcpu-4gb",
  "organization_id": 123
}
```

**Outputs used by app:** `public_ip`, `instance_id`
