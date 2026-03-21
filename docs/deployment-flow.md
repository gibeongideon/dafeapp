# Deployment Flow

This document describes how DafeApp provisions an Odoo server and creates Odoo instances.

---

## 1. Add Infrastructure

Before deploying, the user connects their infrastructure.

### Option A — PYOS (External SSH Server)
1. User goes to **Cloud > Add Server**, enters host/port/credentials.
2. DafeApp SSHes in (via Paramiko) and runs a verification check.
3. User adds the **DafeApp public SSH key** (Ed25519 system key) to the server's `authorized_keys`.
4. DafeApp runs a "prepare" step to confirm connectivity with key-based auth.

### Option B — Managed Cloud (DigitalOcean / AWS)
1. User goes to **Cloud > Add Cloud Account**, enters API token / AWS keys.
2. DafeApp verifies credentials by calling the provider API.
3. Account is stored encrypted in the database.

---

## 2. Create an Infrastructure Record

In **Deployments > Infrastructure**, the user links an org to either a PYOS server or a cloud account. This creates an `Infrastructure` record (`type = PYOS | MANAGED`).

---

## 3. Provision an Odoo Server

The user fills in: Odoo version, region, size (for managed), server name, optional domain.

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
                        executes installer on remote host
```

**OdooServer status progression:**
`PENDING` → `PROVISIONING` → `CONFIGURING` → `PROVISIONED` (or `FAILED`)

### Testing the same flow locally or over SSH

The bare-metal installer used by the UI is the same one you can run from the CLI:

- Local simulation: `sudo ./scripts/deploy_bare.sh --local --fresh --version 19 --port 8069`
- SSH simulation: `./scripts/deploy_bare.sh --ip <server-ip> --user <ssh-user> --key ~/.ssh/<private-key> --version 19 --port 8069`

Local mode is for phase-one validation on your workstation or a disposable VM. SSH mode is the production-like path and is what the DafeApp UI drives for PYOS / bare-metal servers.

### Simulating instance creation with Ansible

After the server is provisioned and Odoo is running, you can simulate instance creation by running the instance playbook directly against the host.

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
            Creates: nginx site + SSL (certbot)
                     /etc/odoo/<db_name>.conf
                     systemd service
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
