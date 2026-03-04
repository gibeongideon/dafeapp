import json
import logging
import os
import socket
import subprocess
import time
from contextlib import suppress
from pathlib import Path

import paramiko
from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer
from django.conf import settings
from django.utils import timezone

from audit.models import AuditLog
from cloud.providers import get_provider
from core.utils import log_audit
from deployments.models import Infrastructure, Instance, OdooInstance, OdooServer, TerraformRun

logger = logging.getLogger(__name__)


def _broadcast_run(run: TerraformRun):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    payload = {
        "id": run.id,
        "status": run.status,
        "output_log": run.output_log[-4000:],
        "error_log": run.error_log[-4000:],
        "instance_id": run.instance_id,
    }
    try:
        async_to_sync(channel_layer.group_send)(
            f"deployments.run.{run.id}",
            {"type": "deployment.update", "payload": payload},
        )
    except Exception:
        logger.warning("Channels broadcast skipped: channel layer unavailable.", exc_info=True)


def _run_cmd(args: list[str], cwd: Path, extra_env: dict | None = None) -> tuple[int, str, str]:
    env = os.environ.copy()
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _append_logs(run: TerraformRun, out: str = "", err: str = ""):
    if out:
        run.output_log = f"{run.output_log}\n{out}".strip()
    if err:
        run.error_log = f"{run.error_log}\n{err}".strip()
    run.save(update_fields=["output_log", "error_log"])
    _broadcast_run(run)


def _append_text(current: str, msg: str) -> str:
    return f"{current}\n{msg}".strip() if current else msg


def _terraform_provider_env(account, region: str = "") -> dict:
    env = {}
    if account is None:
        return env
    if account.provider == "DIGITALOCEAN":
        token = getattr(account, "api_token", "")
        if token:
            env["DIGITALOCEAN_TOKEN"] = token
    elif account.provider == "AWS":
        access_key = getattr(account, "aws_access_key_id", "")
        secret_key = getattr(account, "aws_secret_access_key", "")
        if access_key:
            env["AWS_ACCESS_KEY_ID"] = access_key
        if secret_key:
            env["AWS_SECRET_ACCESS_KEY"] = secret_key
        if region:
            env["AWS_DEFAULT_REGION"] = region
        elif getattr(account, "aws_default_region", ""):
            env["AWS_DEFAULT_REGION"] = account.aws_default_region
    return env


def _apply_ansible(ip: str, run: TerraformRun):
    playbook = os.getenv("ANSIBLE_POST_PROVISION_PLAYBOOK", "").strip()
    if not playbook:
        return True, "Ansible skipped: ANSIBLE_POST_PROVISION_PLAYBOOK is not set."
    code, out, err = _run_cmd(
        ["ansible-playbook", playbook, "-i", f"{ip},"],
        Path(settings.BASE_DIR),
    )
    _append_logs(run, f"[ansible]\n{out}", f"[ansible]\n{err}")
    if code != 0:
        return False, "Ansible post-provision failed."
    return True, "Ansible post-provision completed."


def _test_ssh(ip: str):
    username = os.getenv("TERRAFORM_SSH_USER", "ubuntu")
    password = os.getenv("TERRAFORM_SSH_PASSWORD", "")
    key_path = os.getenv("TERRAFORM_SSH_KEY_PATH", "")
    timeout = int(os.getenv("TERRAFORM_SSH_TIMEOUT", "15"))

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        kwargs = {"hostname": ip, "username": username, "timeout": timeout, "banner_timeout": timeout}
        if key_path:
            kwargs["key_filename"] = key_path
        elif password:
            kwargs["password"] = password
        else:
            return True, "SSH skipped: no TERRAFORM_SSH_KEY_PATH or TERRAFORM_SSH_PASSWORD configured."
        client.connect(**kwargs)
        _, stdout, _ = client.exec_command("echo dafeapp-ok", timeout=timeout)
        out = stdout.read().decode().strip()
        return out == "dafeapp-ok", "SSH validation succeeded." if out == "dafeapp-ok" else "SSH validation failed."
    except (paramiko.SSHException, socket.error, TimeoutError) as exc:
        return False, f"SSH validation failed: {exc}"
    finally:
        client.close()


def _extract_public_ip(workdir: Path, extra_env: dict | None = None):
    code, out, _ = _run_cmd(["terraform", "output", "-json"], workdir, extra_env=extra_env)
    if code != 0 or not out.strip():
        return ""
    try:
        data = json.loads(out)
        for key in ("public_ip", "ip_address", "instance_ip"):
            val = data.get(key, {})
            if isinstance(val, dict) and val.get("value"):
                return str(val["value"])
            if isinstance(val, str):
                return val
    except Exception:
        return ""
    return ""


def _provider_native_provision(instance: Instance, run: TerraformRun) -> tuple[bool, str, str]:
    account = instance.cloud_account
    if account is None:
        return False, "", "No cloud account set on instance."

    provider = get_provider(account)
    ssh_key_ids = provider.list_ssh_keys() or None
    created = provider.create_server(name=instance.name, region=instance.region, size=instance.size, ssh_key_ids=ssh_key_ids)
    provider_id = str(created.get("id") or "")
    if not provider_id:
        return False, "", "Provider did not return a server id."

    provider.create_firewall(provider_id)
    _append_logs(run, f"[provider] server created with id={provider_id}", "")

    for _ in range(30):
        status = provider.get_server_status(provider_id)
        if status in ("active", "running"):
            ip = provider.get_server_ip(provider_id)
            return True, ip, ""
        time.sleep(5)

    return False, "", "Provider provisioning timed out while waiting for running status."


def _provider_native_provision_server(server: OdooServer) -> tuple[bool, str, str, str]:
    if not server.cloud_account:
        return False, "", "", "Infrastructure has no managed cloud account."
    provider = get_provider(server.cloud_account)
    ssh_key_ids = provider.list_ssh_keys()
    if not ssh_key_ids:
        logger.warning(
            "No SSH keys found in cloud account '%s'. "
            "Add an SSH key to your DigitalOcean account so Ansible can connect.",
            server.cloud_account.name,
        )
    created = provider.create_server(name=server.name, region=server.region, size=server.size, ssh_key_ids=ssh_key_ids or None)
    provider_id = str(created.get("id") or "")
    if not provider_id:
        return False, "", "", "Provider did not return a server id."

    provider.create_firewall(provider_id)

    for _ in range(30):
        status = provider.get_server_status(provider_id)
        if status in ("active", "running"):
            ip = provider.get_server_ip(provider_id)
            return True, provider_id, ip, ""
        time.sleep(5)

    return False, provider_id, "", "Provider provisioning timed out while waiting for running status."


def _queue_or_run(task, *args):
    try:
        task.delay(*args)
    except Exception:
        task(*args)


def _run_ansible_playbook(playbook: str, ip: str, extra_vars: dict) -> tuple[bool, str]:
    ssh_user = os.getenv("ANSIBLE_SSH_USER", "root").strip()
    ssh_key = os.getenv("ANSIBLE_SSH_KEY_PATH", "").strip()

    args = [
        "ansible-playbook", playbook,
        "-i", f"{ip},",
        "--user", ssh_user,
        # Disable host-key checking for freshly provisioned servers
        "--ssh-extra-args", "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    ]
    if ssh_key:
        args.extend(["--private-key", ssh_key])

    for key, value in extra_vars.items():
        args.extend(["-e", f"{key}={value}"])

    code, out, err = _run_cmd(
        args,
        Path(settings.BASE_DIR),
        extra_env={"ANSIBLE_HOST_KEY_CHECKING": "False"},
    )
    log_blob = _append_text(out.strip(), err.strip())
    return code == 0, log_blob


@shared_task(bind=True, max_retries=0)
def terraform_apply_instance(self, run_id: int):
    run = TerraformRun.objects.select_related("instance", "instance__organization", "instance__cloud_account").get(pk=run_id)
    instance = run.instance
    org = instance.organization
    account = instance.cloud_account

    run.status = TerraformRun.Status.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at"])
    _broadcast_run(run)

    instance.status = Instance.Status.PENDING
    instance.save(update_fields=["status"])

    state_root = Path(settings.BASE_DIR) / ".terraform_state" / f"org_{org.id}" / f"instance_{instance.id}"
    state_root.mkdir(parents=True, exist_ok=True)

    tf_dir = os.getenv("TERRAFORM_MODULE_DIR", "").strip()
    module_dir = Path(tf_dir) if tf_dir else state_root

    vars_payload = {
        "name": instance.name,
        "region": instance.region,
        "size": instance.size,
        "provider": account.provider if account else "",
        "organization_id": org.id,
    }
    tfvars_path = state_root / "terraform.auto.tfvars.json"
    tfvars_path.write_text(json.dumps(vars_payload, indent=2))

    run.command = f"terraform -chdir={module_dir} init && terraform -chdir={module_dir} apply -auto-approve -var-file={tfvars_path}"
    run.state_file_path = str(module_dir / "terraform.tfstate")
    run.save(update_fields=["command", "state_file_path"])
    _broadcast_run(run)

    if not (module_dir / "main.tf").exists():
        msg = "Terraform module not found. Falling back to provider-native provisioning. Set TERRAFORM_MODULE_DIR to use Terraform."
        _append_logs(run, f"[fallback] {msg}", "")
        ok, ip, err = _provider_native_provision(instance, run)
        if not ok:
            run.status = TerraformRun.Status.FAILED
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "finished_at"])
            instance.status = Instance.Status.FAILED
            instance.provisioning_log = err or msg
            instance.save(update_fields=["status", "provisioning_log"])
            _broadcast_run(run)
            return

        run.status = TerraformRun.Status.SUCCESS
        run.finished_at = timezone.now()
        run.metadata = {"public_ip": ip, "mode": "provider_fallback"}
        run.save(update_fields=["status", "finished_at", "metadata"])

        instance.ip_address = ip or None
        instance.status = Instance.Status.RUNNING
        instance.provisioning_log = msg
        instance.save(update_fields=["ip_address", "status", "provisioning_log"])
        log_audit(
            instance.created_by,
            AuditLog.Action.DROPLET_PROVISION,
            None,
            f"Instance '{instance.name}' provisioned via provider fallback.",
            metadata={"run_id": run.id, "instance_id": instance.id, "public_ip": ip},
            organization=org,
        )
        _broadcast_run(run)
        return

    tf_env = _terraform_provider_env(account, instance.region)
    code, out, err = _run_cmd(["terraform", f"-chdir={module_dir}", "init", "-input=false"], module_dir, extra_env=tf_env)
    _append_logs(run, f"[terraform init]\n{out}", f"[terraform init]\n{err}")
    if code != 0:
        run.status = TerraformRun.Status.FAILED
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "finished_at"])
        instance.status = Instance.Status.FAILED
        instance.provisioning_log = "Terraform init failed."
        instance.save(update_fields=["status", "provisioning_log"])
        _broadcast_run(run)
        return

    code, out, err = _run_cmd(
        [
            "terraform",
            f"-chdir={module_dir}",
            "apply",
            "-auto-approve",
            "-input=false",
            f"-var-file={tfvars_path}",
        ],
        module_dir,
        extra_env=tf_env,
    )
    _append_logs(run, f"[terraform apply]\n{out}", f"[terraform apply]\n{err}")
    if code != 0:
        run.status = TerraformRun.Status.FAILED
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "finished_at"])
        instance.status = Instance.Status.FAILED
        instance.provisioning_log = "Terraform apply failed."
        instance.terraform_state_path = str(module_dir / "terraform.tfstate")
        instance.save(update_fields=["status", "provisioning_log", "terraform_state_path"])
        _broadcast_run(run)
        return

    ip = _extract_public_ip(module_dir, extra_env=tf_env)
    ok_ansible, ansible_msg = _apply_ansible(ip, run) if ip else (True, "No public IP output; Ansible skipped.")
    _append_logs(run, ansible_msg, "")

    ok_ssh, ssh_msg = _test_ssh(ip) if ip else (True, "SSH validation skipped: no public IP.")
    _append_logs(run, ssh_msg, "")

    run.status = TerraformRun.Status.SUCCESS if (ok_ansible and ok_ssh) else TerraformRun.Status.FAILED
    run.finished_at = timezone.now()
    run.metadata = {"public_ip": ip}
    run.save(update_fields=["status", "finished_at", "metadata"])

    instance.ip_address = ip or None
    instance.status = Instance.Status.RUNNING if run.status == TerraformRun.Status.SUCCESS else Instance.Status.FAILED
    instance.provisioning_log = f"{ansible_msg}\n{ssh_msg}".strip()
    instance.terraform_state_path = str(module_dir / "terraform.tfstate")
    instance.save(update_fields=["ip_address", "status", "provisioning_log", "terraform_state_path"])

    action = AuditLog.Action.DROPLET_PROVISION if instance.status == Instance.Status.RUNNING else AuditLog.Action.OTHER
    log_audit(
        instance.created_by,
        action,
        None,
        f"Terraform provision for instance '{instance.name}' finished with status {instance.status}.",
        metadata={"run_id": run.id, "instance_id": instance.id, "public_ip": ip},
        organization=org,
    )
    _broadcast_run(run)


@shared_task(bind=True, max_retries=0)
def provision_odoo_server(self, server_id: int):
    server = OdooServer.objects.select_related(
        "organization",
        "cloud_account",
        "infrastructure",
        "infrastructure__cloud_account",
        "infrastructure__external_server",
    ).get(pk=server_id)
    org = server.organization

    server.status = OdooServer.Status.PROVISIONING
    server.save(update_fields=["status", "updated_at"])

    infra = server.infrastructure
    if not infra:
        server.status = OdooServer.Status.FAILED
        server.provisioning_log = _append_text(server.provisioning_log, "Server is missing infrastructure.")
        server.save(update_fields=["status", "provisioning_log", "updated_at"])
        return

    ok, err = infra.validate_connection_target()
    if not ok:
        server.status = OdooServer.Status.FAILED
        server.provisioning_log = _append_text(server.provisioning_log, err)
        server.save(update_fields=["status", "provisioning_log", "updated_at"])
        return

    managed_account = server.effective_cloud_account
    if managed_account and not server.cloud_account_id:
        server.cloud_account = managed_account
        server.save(update_fields=["cloud_account"])

    if infra.infra_type == Infrastructure.InfraType.PYOS:
        ext = infra.external_server
        server.ip_address = ext.host
        server.firewall_configured = True
        server.provisioning_log = _append_text(
            server.provisioning_log,
            "Using PYOS infrastructure connection. Compute already exists; proceeding to configuration.",
        )
        server.status = OdooServer.Status.CONFIGURING
        server.save(
            update_fields=["ip_address", "firewall_configured", "provisioning_log", "status", "updated_at"]
        )
        _queue_or_run(configure_odoo_server, server.id)
        return

    state_root = Path(settings.BASE_DIR) / ".terraform_state" / f"org_{org.id}" / f"odoo_server_{server.id}"
    state_root.mkdir(parents=True, exist_ok=True)

    tf_dir = os.getenv("TERRAFORM_SERVER_MODULE_DIR", "").strip()
    module_dir = Path(tf_dir) if tf_dir else state_root

    vars_payload = {
        "name": server.name,
        "region": server.region,
        "size": server.size,
        "provider": managed_account.provider if managed_account else "",
        "organization_id": org.id,
        "odoo_version": server.odoo_version,
    }
    tfvars_path = state_root / "terraform.auto.tfvars.json"
    tfvars_path.write_text(json.dumps(vars_payload, indent=2))
    server.terraform_state_path = str(module_dir / "terraform.tfstate")
    server.save(update_fields=["terraform_state_path"])

    tf_env = _terraform_provider_env(managed_account, server.region)

    if (module_dir / "main.tf").exists():
        code, out, err = _run_cmd(["terraform", f"-chdir={module_dir}", "init", "-input=false"], module_dir, extra_env=tf_env)
        server.provisioning_log = _append_text(server.provisioning_log, f"[terraform init]\n{out}\n{err}".strip())
        server.save(update_fields=["provisioning_log"])
        if code != 0:
            server.status = OdooServer.Status.FAILED
            server.save(update_fields=["status", "updated_at"])
            return

        code, out, err = _run_cmd(
            [
                "terraform",
                f"-chdir={module_dir}",
                "apply",
                "-auto-approve",
                "-input=false",
                f"-var-file={tfvars_path}",
            ],
            module_dir,
            extra_env=tf_env,
        )
        server.provisioning_log = _append_text(server.provisioning_log, f"[terraform apply]\n{out}\n{err}".strip())
        server.save(update_fields=["provisioning_log"])
        if code != 0:
            server.status = OdooServer.Status.FAILED
            server.save(update_fields=["status", "updated_at"])
            return

        ip = _extract_public_ip(module_dir, extra_env=tf_env)
        if not ip:
            ok, provider_id, ip, err = _provider_native_provision_server(server)
            if not ok:
                server.status = OdooServer.Status.FAILED
                server.provisioning_log = _append_text(server.provisioning_log, err)
                server.save(update_fields=["status", "provisioning_log", "updated_at"])
                return
            server.provider_server_id = provider_id
        else:
            server.firewall_configured = True
        server.ip_address = ip or None
    else:
        ok, provider_id, ip, err = _provider_native_provision_server(server)
        if not ok:
            server.status = OdooServer.Status.FAILED
            server.provisioning_log = _append_text(server.provisioning_log, "Provider fallback provisioning failed.")
            server.provisioning_log = _append_text(server.provisioning_log, err)
            server.save(update_fields=["status", "provisioning_log", "updated_at"])
            return
        server.provider_server_id = provider_id
        server.ip_address = ip or None
        server.firewall_configured = True
        server.provisioning_log = _append_text(
            server.provisioning_log,
            "Provisioned with provider API fallback (no Terraform module found).",
        )

    dns_hook = os.getenv("DNS_CREATE_HOOK_CMD", "").strip()
    if dns_hook and server.dns_domain and server.ip_address:
        code, out, err = _run_cmd([dns_hook, server.dns_domain, str(server.ip_address)], Path(settings.BASE_DIR))
        server.provisioning_log = _append_text(server.provisioning_log, f"[dns hook]\n{out}\n{err}".strip())
        if code != 0:
            server.provisioning_log = _append_text(server.provisioning_log, "DNS hook failed (non-blocking).")

    server.status = OdooServer.Status.CONFIGURING
    server.save(
        update_fields=[
            "status",
            "provider_server_id",
            "ip_address",
            "firewall_configured",
            "provisioning_log",
            "updated_at",
        ]
    )

    _queue_or_run(configure_odoo_server, server.id)


@shared_task(bind=True, max_retries=0)
def configure_odoo_server(self, server_id: int):
    server = OdooServer.objects.select_related("organization").get(pk=server_id)
    if not server.ip_address:
        server.status = OdooServer.Status.FAILED
        server.provisioning_log = _append_text(server.provisioning_log, "No server IP available for configuration.")
        server.save(update_fields=["status", "provisioning_log", "updated_at"])
        return

    playbook = os.getenv("ANSIBLE_ODOO_SERVER_PLAYBOOK", "").strip()
    if not playbook:
        server.status = OdooServer.Status.PROVISIONED
        server.provisioning_log = _append_text(
            server.provisioning_log,
            "ANSIBLE_ODOO_SERVER_PLAYBOOK not set. Marked PROVISIONED without server bootstrap.",
        )
        server.save(update_fields=["status", "provisioning_log", "updated_at"])
        return

    # Build extra-vars for the playbook.
    # setup_odoo_server_bare.yml needs dns_domain → website_name mapping and
    # an admin_email for certbot. The original setup_odoo_server.yml only uses
    # odoo_version / server_name / dns_domain, so the extra keys are ignored.
    admin_email = os.getenv("ODOO_ADMIN_EMAIL", "odoo@example.com").strip()
    extra_vars = {
        "odoo_version": server.odoo_version,
        "server_name": server.name,
        "dns_domain": server.dns_domain,
        # Bare-metal playbook extras:
        "website_name": server.dns_domain if server.dns_domain else "_",
        "admin_email": admin_email,
    }

    ok, log_blob = _run_ansible_playbook(
        playbook,
        str(server.ip_address),
        extra_vars,
    )
    server.provisioning_log = _append_text(server.provisioning_log, f"[ansible server]\n{log_blob}".strip())
    server.status = OdooServer.Status.PROVISIONED if ok else OdooServer.Status.FAILED
    server.save(update_fields=["status", "provisioning_log", "updated_at"])

    log_audit(
        server.created_by,
        AuditLog.Action.OTHER,
        None,
        f"Odoo server '{server.name}' configuration finished with status {server.status}.",
        metadata={"server_id": server.id, "odoo_version": server.odoo_version, "ip": server.ip_address},
        organization=server.organization,
    )


@shared_task(bind=True, max_retries=0)
def create_odoo_instance(self, instance_id: int):
    instance = OdooInstance.objects.select_related("organization", "server").get(pk=instance_id)
    server = instance.server
    if server.status != OdooServer.Status.PROVISIONED or not server.ip_address:
        instance.status = OdooInstance.Status.FAILED
        instance.provisioning_log = "Server is not ready for instance creation."
        instance.save(update_fields=["status", "provisioning_log", "updated_at"])
        return

    instance.status = OdooInstance.Status.CONFIGURING
    instance.save(update_fields=["status", "updated_at"])

    # Choose playbook:
    #   - direct (no nginx/domain): ANSIBLE_ODOO_INSTANCE_DIRECT_PLAYBOOK
    #   - domain-based (nginx + SSL): ANSIBLE_ODOO_INSTANCE_PLAYBOOK
    direct_playbook = os.getenv("ANSIBLE_ODOO_INSTANCE_DIRECT_PLAYBOOK", "").strip()
    domain_playbook = os.getenv("ANSIBLE_ODOO_INSTANCE_PLAYBOOK", "").strip()
    use_direct = not instance.domain and bool(direct_playbook)
    playbook = direct_playbook if use_direct else domain_playbook

    if not playbook:
        instance.systemd_service = f"odoo-{instance.db_name}"
        instance.nginx_site = ""
        instance.ssl_enabled = False
        instance.status = OdooInstance.Status.RUNNING
        instance.provisioning_log = "No Ansible playbook configured. Marked RUNNING with generated metadata."
        instance.save(
            update_fields=[
                "systemd_service",
                "nginx_site",
                "ssl_enabled",
                "status",
                "provisioning_log",
                "updated_at",
            ]
        )
        return

    extra_vars = {
        "odoo_version": server.odoo_version,
        "db_name": instance.db_name,
        "instance_name": instance.name,
        "http_port": instance.http_port,
    }
    if not use_direct:
        extra_vars["domain"] = instance.domain

    ok, log_blob = _run_ansible_playbook(playbook, str(server.ip_address), extra_vars)
    instance.provisioning_log = f"[ansible instance]\n{log_blob}".strip()
    if ok:
        instance.status = OdooInstance.Status.RUNNING
        instance.systemd_service = f"odoo-{instance.db_name}"
        instance.nginx_site = "" if use_direct else (instance.domain or f"{instance.name}.local")
        instance.ssl_enabled = False if use_direct else bool(instance.domain)
    else:
        instance.status = OdooInstance.Status.FAILED
    instance.save(
        update_fields=[
            "status",
            "systemd_service",
            "nginx_site",
            "ssl_enabled",
            "provisioning_log",
            "updated_at",
        ]
    )


@shared_task(bind=True, max_retries=0)
def delete_odoo_instance(self, instance_id: int):
    """Run the direct-IP deletion playbook then mark the instance DELETED."""
    instance = OdooInstance.objects.select_related("organization", "server").get(pk=instance_id)
    server = instance.server

    if not server.ip_address:
        # Server IP gone (server deleted); nothing to clean up remotely.
        instance.status = OdooInstance.Status.DELETED
        instance.provisioning_log = _append_text(instance.provisioning_log, "Server IP unavailable; skipped remote cleanup.")
        instance.save(update_fields=["status", "provisioning_log", "updated_at"])
        return

    playbook = os.getenv("ANSIBLE_ODOO_INSTANCE_DELETE_PLAYBOOK", "").strip()
    if not playbook:
        instance.status = OdooInstance.Status.DELETED
        instance.provisioning_log = _append_text(instance.provisioning_log, "No delete playbook configured; marked DELETED.")
        instance.save(update_fields=["status", "provisioning_log", "updated_at"])
        return

    ok, log_blob = _run_ansible_playbook(
        playbook,
        str(server.ip_address),
        {
            "db_name": instance.db_name,
            "http_port": instance.http_port,
        },
    )
    instance.provisioning_log = _append_text(instance.provisioning_log, f"[ansible delete]\n{log_blob}")
    instance.status = OdooInstance.Status.DELETED
    instance.save(update_fields=["status", "provisioning_log", "updated_at"])


def _tcp_reachable(host: str, port: int, timeout: int = 5) -> bool:
    """Return True if a TCP connection to host:port succeeds within timeout seconds."""
    with suppress(OSError):
        with socket.create_connection((host, port), timeout=timeout):
            return True
    return False


@shared_task
def check_server_connectivity():
    """
    Periodic task: TCP-probe port 22 on every active OdooServer and ExternalServer.
    Updates is_reachable + last_checked_at without touching other fields.
    """
    from cloud.models import ExternalServer
    from django.utils import timezone

    now = timezone.now()

    # --- OdooServer: probe SSH port 22 ---
    servers = OdooServer.objects.filter(
        ip_address__isnull=False,
    ).exclude(status=OdooServer.Status.DELETED)

    for server in servers:
        reachable = _tcp_reachable(str(server.ip_address), 22)
        server.is_reachable = reachable
        server.last_checked_at = now
        server.save(update_fields=["is_reachable", "last_checked_at"])

    # --- ExternalServer: probe the configured SSH port ---
    ext_servers = ExternalServer.objects.filter(host__isnull=False)

    for ext in ext_servers:
        reachable = _tcp_reachable(str(ext.host), ext.port or 22)
        ext.is_reachable = reachable
        ext.last_checked_at = now
        ext.save(update_fields=["is_reachable", "last_checked_at"])
