# Odoo Ansible Starters

Playbooks included:

- `setup_odoo_server.yml`
  - Installs base stack (Python, PostgreSQL, Nginx, UFW, certbot)
  - Clones Odoo source for the requested `odoo_version`
  - Prepares filesystem/user/base config

- `create_odoo_instance.yml`
  - Creates one DB-backed Odoo instance on an existing version server
  - Renders instance config + systemd service + nginx site
  - Optionally requests Let's Encrypt cert when `domain` is provided

Variables passed by DafeApp task layer:

- Server playbook:
  - `odoo_version`
  - `server_name`
  - `dns_domain`

- Instance playbook:
  - `odoo_version`
  - `db_name`
  - `instance_name`
  - `domain`
  - `http_port`

Notes:

- These are starter playbooks; production hardening is still required.
- Ensure target hosts are Ubuntu-like and reachable by SSH from the app/worker host.
