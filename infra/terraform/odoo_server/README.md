# Odoo Server Terraform Starter

This module provisions one version-specific server for Odoo (`18` or `19`).

Supported providers:
- `DIGITALOCEAN`
- `AWS`

Credential behavior:
- DigitalOcean provider reads token from `DIGITALOCEAN_TOKEN` environment variable.
- AWS provider reads credentials from standard AWS env vars (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optionally `AWS_SESSION_TOKEN`).

Expected vars are written by DafeApp task into `terraform.auto.tfvars.json`:
- `provider`
- `name`
- `odoo_version`
- `region`
- `size`
- `organization_id`

Outputs used by DafeApp:
- `instance_id`
- `public_ip`
