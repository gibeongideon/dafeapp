import json
import logging
import os
import re
import socket
import subprocess
import tempfile
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
from deployments.models import (
    DeploymentJob,
    Infrastructure,
    Instance,
    OdooInstance,
    OdooInstanceHistory,
    OdooServer,
    OdooServerHistory,
    TerraformRun,
)

logger = logging.getLogger(__name__)


def _broadcast_server(
    server_id: int,
    step: str,
    status: str,
    done: bool = False,
    log: str = "",
    summary: dict | None = None,
):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    payload = {"type": "done" if done else "step", "step": step, "status": status, "log": log[-2000:] if log else ""}
    if summary is not None:
        payload["summary"] = summary
    try:
        async_to_sync(channel_layer.group_send)(
            f"odoo.server.{server_id}",
            {"type": "server.update", "payload": payload},
        )
    except Exception:
        logger.warning("Server broadcast skipped for server %s", server_id, exc_info=True)


def _broadcast_server_snapshot(server: OdooServer):
    """Push a full server snapshot to any open websocket listeners."""
    try:
        from deployments.serializers import OdooServerSerializer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            f"odoo.server.{server.id}",
            {
                "type": "server.update",
                "payload": {
                    "type": "snapshot",
                    "server": OdooServerSerializer(server).data,
                },
            },
        )
    except Exception:
        logger.warning("Server snapshot broadcast skipped for server %s", server.id, exc_info=True)


def _broadcast_instance(
    instance_id: int,
    step: str,
    status: str,
    done: bool = False,
    log: str = "",
    summary: dict | None = None,
):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    payload = {"type": "done" if done else "step", "step": step, "status": status, "log": log[-2000:] if log else ""}
    if summary is not None:
        payload["summary"] = summary
    try:
        async_to_sync(channel_layer.group_send)(
            f"odoo.instance.{instance_id}",
            {"type": "instance.update", "payload": payload},
        )
    except Exception:
        logger.warning("Instance broadcast skipped for instance %s", instance_id, exc_info=True)


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


def _broadcast_log_line(group: str, line: str):
    """Send a single streamed log line to a WebSocket group."""
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            group,
            {"type": "log.line", "payload": {"type": "log_line", "line": line.rstrip()}},
        )
    except Exception:
        pass


def _job_start(job_id: int | None, celery_task_id: str):
    if not job_id:
        return
    DeploymentJob.objects.filter(pk=job_id).update(
        celery_task_id=celery_task_id,
        status=DeploymentJob.Status.RUNNING,
        started_at=timezone.now(),
    )


def _job_done(job_id: int | None, ok: bool, log: str = ""):
    if not job_id:
        return
    DeploymentJob.objects.filter(pk=job_id).update(
        status=DeploymentJob.Status.DONE if ok else DeploymentJob.Status.FAILED,
        log=log[-8000:],
        finished_at=timezone.now(),
    )


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


def _record_instance_progress(instance: OdooInstance, message: str):
    instance.provisioning_log = _append_text(instance.provisioning_log, message)
    instance.save(update_fields=["provisioning_log", "updated_at"])


def _extract_admin_password(log_blob: str) -> str:
    """Pull the generated admin password from ansible / shell summary output."""
    if not log_blob:
        return ""
    patterns = (
        r"Admin password:\s*(.+)",
        r"admin_passwd\s*=\s*(.+)",
    )
    for pattern in patterns:
        match = re.search(pattern, log_blob, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _build_installation_summary_text(
    *,
    server: OdooServer,
    ssh_user: str,
) -> str:
    """Format the environment bootstrap summary shown in the UI."""
    ip = str(server.ip_address or "")
    lines = [
        "============================================================",
        f"  Odoo {server.odoo_version} environment ready!",
        "============================================================",
        f"  Server IP     : {ip or '(not available)'}",
        "  Source code   : /odoo/odoo-server",
        "  Python venv   : /odoo/venv",
        "  Log directory : /var/log/odoo",
    ]
    if ssh_user:
        lines.append(f"  SSH user      : {ssh_user}")
    lines.extend(
        [
            "  PostgreSQL    : ready for instance creation",
            "",
            "  Next step:",
            "    Create an Odoo instance with infra/ansible/create_odoo_instance_direct.yml",
            "",
            "  No standalone Odoo service was started.",
        ]
    )
    lines.extend(
        [
            "============================================================",
        ]
    )
    return "\n".join(lines)


def _build_instance_installation_summary_text(
    *,
    instance: OdooInstance,
    server: OdooServer,
    playbook: str,
    ssh_user: str,
    use_direct: bool,
) -> str:
    """Format the per-instance install summary shown in the UI."""
    playbook_name = Path(playbook).name
    server_ip = str(server.ip_address or "")
    access_url = instance.access_url or (
        f"https://{instance.domain}" if instance.domain and server.deployment_mode == OdooServer.DeploymentMode.DOCKER else ""
    )
    service_name = instance.systemd_service or f"odoo-{instance.db_name}"

    if use_direct:
        instance_root = f"/odoo/instances/{instance.db_name}"
        lines = [
            "============================================================",
            "  Odoo instance created (fully isolated)",
            "============================================================",
            f"  Instance     : {instance.name}",
            f"  Database     : {instance.db_name}",
            f"  Service      : {service_name}",
            f"  Port         : {instance.http_port}",
            f"  Server IP    : {server_ip or '(not available)'}",
            f"  Playbook     : {playbook_name}",
            "  Source code  : /odoo/odoo-server",
            f"  Python venv  : {instance_root}/venv",
            f"  Core addons  : {instance_root}/addons/core",
            f"  Custom addons: {instance_root}/addons/custom",
            f"  Data dir     : {instance_root}/data",
            f"  Logs         : {instance_root}/logs/{instance.db_name}.log",
            f"  Config       : /etc/odoo-{instance.db_name}.conf",
            f"  SSH user     : {ssh_user or 'root'}",
            f"  Access       : {access_url or 'pending'}",
            "============================================================",
        ]
        return "\n".join(lines)

    odoo_home = f"/opt/odoo{server.odoo_version}"
    instance_root = f"{odoo_home}/instances/{instance.db_name}"
    lines = [
        "============================================================",
        "  Odoo instance created",
        "============================================================",
        f"  Instance     : {instance.name}",
        f"  Database     : {instance.db_name}",
        f"  Service      : {service_name}",
        f"  Port         : {instance.http_port}",
        f"  Server IP    : {server_ip or '(not available)'}",
        f"  Playbook     : {playbook_name}",
        f"  Source code  : {odoo_home}/src/odoo",
        f"  Addons path  : {odoo_home}/src/odoo/addons",
        f"  Instance dir : {instance_root}",
        f"  Logs         : {odoo_home}/logs/{instance.db_name}.log",
        f"  Config       : {instance_root}/odoo.conf",
        f"  Nginx site   : /etc/nginx/sites-available/odoo-{instance.db_name}.conf",
        f"  SSH user     : {ssh_user or 'root'}",
        f"  Domain       : {instance.domain or 'not set'}",
        f"  Access       : {access_url or 'pending'}",
        "============================================================",
    ]
    return "\n".join(lines)


def _store_installation_summary(
    server: OdooServer,
    *,
    ssh_user: str,
) -> tuple[dict, str]:
    summary = {
        "server_ip": str(server.ip_address or ""),
        "odoo_version": server.odoo_version,
        "mode": "environment",
        "source_dir": "/odoo/odoo-server",
        "venv_dir": "/odoo/venv",
        "log_dir": "/var/log/odoo",
        "ssh_user": ssh_user,
        "service_commands": [],
        "open_urls": [],
        "next_step": "create an Odoo instance with infra/ansible/create_odoo_instance_direct.yml",
    }
    summary_text = _build_installation_summary_text(
        server=server,
        ssh_user=ssh_user,
    )
    server.installation_summary = summary
    server.installation_summary_text = summary_text
    server.save(update_fields=["installation_summary", "installation_summary_text", "updated_at"])
    return summary, summary_text


def _store_instance_installation_summary(
    instance: OdooInstance,
    *,
    server: OdooServer,
    playbook: str,
    ssh_user: str,
    use_direct: bool,
) -> tuple[dict, str]:
    summary = {
        "summary_type": "instance",
        "server_ip": str(server.ip_address or ""),
        "odoo_version": server.odoo_version,
        "mode": "direct" if use_direct else "domain",
        "playbook": Path(playbook).name,
        "playbook_path": playbook,
        "instance_name": instance.name,
        "database": instance.db_name,
        "service_name": instance.systemd_service or f"odoo-{instance.db_name}",
        "port": instance.http_port,
        "domain": instance.domain or "",
        "access_url": instance.access_url or (
            f"https://{instance.domain}" if instance.domain and server.deployment_mode == OdooServer.DeploymentMode.DOCKER else ""
        ),
        "source_dir": "/odoo/odoo-server" if use_direct else f"/opt/odoo{server.odoo_version}/src/odoo",
        "ssh_user": ssh_user,
    }
    if use_direct:
        instance_root = f"/odoo/instances/{instance.db_name}"
        summary.update(
            {
                "instance_dir": instance_root,
                "venv_dir": f"{instance_root}/venv",
                "core_addons_dir": f"{instance_root}/addons/core",
                "custom_addons_dir": f"{instance_root}/addons/custom",
                "data_dir": f"{instance_root}/data",
                "log_dir": f"{instance_root}/logs",
                "config_file": f"/etc/odoo-{instance.db_name}.conf",
            }
        )
    else:
        odoo_home = f"/opt/odoo{server.odoo_version}"
        instance_root = f"{odoo_home}/instances/{instance.db_name}"
        summary.update(
            {
                "instance_dir": instance_root,
                "venv_dir": f"{odoo_home}/venv",
                "addons_dir": f"{odoo_home}/src/odoo/addons",
                "log_dir": f"{odoo_home}/logs/{instance.db_name}",
                "config_file": f"{instance_root}/odoo.conf",
                "nginx_site": f"/etc/nginx/sites-available/odoo-{instance.db_name}.conf",
            }
        )

    summary_text = _build_instance_installation_summary_text(
        instance=instance,
        server=server,
        playbook=playbook,
        ssh_user=ssh_user,
        use_direct=use_direct,
    )
    instance.installation_summary = summary
    instance.installation_summary_text = summary_text
    instance.save(update_fields=["installation_summary", "installation_summary_text", "updated_at"])
    return summary, summary_text


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
    from cloud.models import SystemSSHKey
    provider = get_provider(server.cloud_account)
    system_key = SystemSSHKey.get_or_create_keypair()
    fingerprint = provider.ensure_dafeapp_ssh_key(system_key.public_key)
    if not fingerprint:
        logger.warning(
            "Could not register DafeApp SSH key in cloud account '%s'. "
            "Ansible may not be able to connect to the new server.",
            server.cloud_account.name,
        )
    ssh_key_ids = [fingerprint] if fingerprint else None
    created = provider.create_server(name=server.name, region=server.region, size=server.size, ssh_key_ids=ssh_key_ids)
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


def _run_ansible_playbook(
    playbook: str,
    ip: str,
    extra_vars: dict,
    ssh_user: str | None = None,
    ssh_key_path: str | None = None,
    ssh_password: str | None = None,
    on_chunk=None,
) -> tuple[bool, str]:
    """
    Run an Ansible playbook against `ip`.

    When `on_chunk` is provided (callable accepting a str line), output is
    streamed line-by-line via Popen so callers can broadcast live to WebSocket.
    Without it, falls back to the original blocking subprocess.run behaviour.
    """
    effective_user = ssh_user or os.getenv("ANSIBLE_SSH_USER", "root").strip()
    effective_key = ssh_key_path or os.getenv("ANSIBLE_SSH_KEY_PATH", "").strip()

    args = [
        "ansible-playbook", playbook,
        "-i", f"{ip},",
        "--user", effective_user,
        "--ssh-extra-args", "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    ]
    if effective_key:
        args.extend(["--private-key", effective_key])

    merged_vars = dict(extra_vars)
    if ssh_password and not effective_key:
        merged_vars["ansible_ssh_pass"] = ssh_password

    for key, value in merged_vars.items():
        args.extend(["-e", f"{key}={value}"])

    env = os.environ.copy()
    env["ANSIBLE_HOST_KEY_CHECKING"] = "False"

    if on_chunk is not None:
        # Streaming mode: read stdout+stderr line by line and call on_chunk.
        proc = subprocess.Popen(
            args,
            cwd=str(Path(settings.BASE_DIR)),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
        )
        lines: list[str] = []
        for line in proc.stdout:
            clean = line.rstrip()
            if clean:
                logger.info("[ansible] %s", clean)
            lines.append(line)
            try:
                on_chunk(line)
            except Exception:
                pass
        proc.wait()
        log_blob = "".join(lines).strip()
        return proc.returncode == 0, log_blob

    # Non-streaming (original) mode.
    code, out, err = _run_cmd(args, Path(settings.BASE_DIR), extra_env={"ANSIBLE_HOST_KEY_CHECKING": "False"})
    log_blob = _append_text(out.strip(), err.strip())
    return code == 0, log_blob


def _default_odoo_server_playbook() -> str:
    """Absolute path to the repo-local bare-metal server bootstrap playbook."""
    return str(Path(settings.BASE_DIR) / "infra" / "ansible" / "setup_odoo_server_bare.yml")


def _default_odoo_instance_direct_playbook() -> str:
    """Absolute path to the repo-local direct-IP instance playbook."""
    return str(Path(settings.BASE_DIR) / "infra" / "ansible" / "create_odoo_instance_direct.yml")


def _default_odoo_instance_playbook() -> str:
    """Absolute path to the repo-local domain-based instance playbook."""
    return str(Path(settings.BASE_DIR) / "infra" / "ansible" / "create_odoo_instance.yml")


def _default_docker_instance_playbook() -> str:
    """Absolute path to the repo-local Docker instance playbook."""
    return str(Path(settings.BASE_DIR) / "infra" / "ansible" / "create_docker_odoo_instance.yml")


def _pyos_ssh_creds(ext_server) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Extract SSH creds from an ExternalServer for use with Ansible.
    Returns (ssh_user, key_path, password, temp_key_file).
    temp_key_file is a path to a NamedTemporaryFile the caller must delete.
    """
    from cloud.encryption import FieldEncryptor
    from cloud.pyos import resolve_private_key_string

    user = ext_server.username or None
    if ext_server.auth_type == "DAFEAPP_KEY":
        private_key_str, _ = resolve_private_key_string(ext_server)
        if not private_key_str:
            return user, None, None, None
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
        tmp.write(private_key_str)
        tmp.close()
        os.chmod(tmp.name, 0o600)
        return user, tmp.name, None, tmp.name
    else:
        password = FieldEncryptor.decrypt(ext_server.encrypted_password)
        return user, None, password, None


def _server_ansible_creds(server) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Return (ssh_user, key_path, password, temp_key_file) for an OdooServer.
    - PYOS servers: use per-server credentials from ExternalServer.
    - MANAGED (cloud) servers: use DafeApp's SystemSSHKey (same key injected at droplet creation).
    temp_key_file must be deleted by the caller when no longer needed.
    """
    try:
        infra = server.infrastructure
        if infra and infra.infra_type == Infrastructure.InfraType.PYOS and infra.external_server:
            return _pyos_ssh_creds(infra.external_server)
        if infra and infra.infra_type == Infrastructure.InfraType.MANAGED:
            from cloud.models import SystemSSHKey
            system_key = SystemSSHKey.get_or_create_keypair()
            private_key_str = system_key.get_private_key()
            if private_key_str:
                tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
                tmp.write(private_key_str)
                tmp.close()
                os.chmod(tmp.name, 0o600)
                ssh_user = os.getenv("ANSIBLE_SSH_USER", "root").strip()
                return ssh_user, tmp.name, None, tmp.name
    except Exception:
        logger.warning("Could not extract SSH creds for server %s", server.pk, exc_info=True)
    return None, None, None, None


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
    server.installation_summary = {}
    server.installation_summary_text = ""
    server.provisioning_log = ""
    server.save(update_fields=["status", "installation_summary", "installation_summary_text", "provisioning_log", "updated_at"])
    infra = server.infrastructure
    logger.info(
        "Server provisioning started: id=%s name=%s version=%s mode=%s infra=%s",
        server.id,
        server.name,
        server.odoo_version,
        server.deployment_mode,
        getattr(infra, "infra_type", "unknown"),
    )
    _broadcast_server(server.id, "Starting provisioning…", server.status)
    if not infra:
        logger.error("Server %s provisioning aborted: missing infrastructure record.", server.id)
        server.status = OdooServer.Status.FAILED
        server.provisioning_log = _append_text(server.provisioning_log, "Server is missing infrastructure.")
        server.save(update_fields=["status", "provisioning_log", "updated_at"])
        _broadcast_server(server.id, "Failed: server is missing infrastructure.", server.status, done=True)
        return

    managed_account = server.effective_cloud_account
    if managed_account and not server.cloud_account_id:
        server.cloud_account = managed_account
        server.save(update_fields=["cloud_account"])

    if infra.infra_type == Infrastructure.InfraType.PYOS:
        ext = infra.external_server
        from cloud.pyos import PyOSService
        from django.utils import timezone

        logger.info(
            "Server %s: validating PYOS reachability for %s:%s",
            server.id,
            ext.host,
            ext.port or 22,
        )
        ext.last_verified_at = None
        ext.verification_error = "Reachability is being verified..."
        ext.save(update_fields=["last_verified_at", "verification_error"])
        _broadcast_server(server.id, "Validating reachability…", server.status)
        reachable, err = PyOSService(ext).validate()
        now = timezone.now()
        ext.is_verified = reachable
        ext.verification_error = "" if reachable else err
        ext.last_verified_at = now
        ext.save(update_fields=["is_verified", "verification_error", "last_verified_at"])

        server.ip_address = ext.host
        server.is_reachable = reachable
        server.last_checked_at = now
        server.firewall_configured = True
        server.provisioning_log = _append_text(server.provisioning_log, "Using PYOS infrastructure connection.")
        server.provisioning_log = _append_text(server.provisioning_log, "Reachability verified." if reachable else err)
        server.save(
            update_fields=[
                "ip_address",
                "is_reachable",
                "last_checked_at",
                "firewall_configured",
                "provisioning_log",
                "updated_at",
            ]
        )
        if not reachable:
            logger.warning("Server %s: PYOS reachability failed: %s", server.id, err)
            server.status = OdooServer.Status.FAILED
            server.save(update_fields=["status", "provisioning_log", "updated_at"])
            _broadcast_server(server.id, f"Failed: {err}", server.status, done=True)
            return

        logger.info(
            "Server %s: PYOS reachability confirmed for %s:%s",
            server.id,
            ext.host,
            ext.port or 22,
        )
        server.status = OdooServer.Status.CONFIGURING
        server.save(
            update_fields=["status", "updated_at"]
        )
        _broadcast_server(server.id, f"Reachability confirmed ({ext.host}) — starting Odoo configuration…", server.status)
        logger.info(
            "Server %s: starting %s configuration",
            server.id,
            "Docker" if server.deployment_mode == OdooServer.DeploymentMode.DOCKER else "Ansible Odoo",
        )
        if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
            _queue_or_run(configure_docker_host, server.id)
        else:
            _queue_or_run(configure_odoo_server, server.id)
        return

    logger.info("Server %s: validating managed infrastructure connection target", server.id)
    _broadcast_server(server.id, "Validating infrastructure connection…", server.status)
    ok, err = infra.validate_connection_target()
    if not ok:
        logger.warning("Server %s: infrastructure validation failed: %s", server.id, err)
        server.status = OdooServer.Status.FAILED
        server.provisioning_log = _append_text(server.provisioning_log, err)
        server.save(update_fields=["status", "provisioning_log", "updated_at"])
        _broadcast_server(server.id, f"Failed: {err}", server.status, done=True)
        return
    logger.info("Server %s: managed infrastructure connection confirmed", server.id)

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

    if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
        _queue_or_run(configure_docker_host, server.id)
    else:
        _queue_or_run(configure_odoo_server, server.id)


@shared_task(bind=True, max_retries=0)
def configure_odoo_server(self, server_id: int, job_id: int | None = None):
    server = OdooServer.objects.select_related(
        "organization",
        "infrastructure",
        "infrastructure__external_server",
    ).get(pk=server_id)

    _job_start(job_id, self.request.id)
    server.installation_summary = {}
    server.installation_summary_text = ""
    server.save(update_fields=["installation_summary", "installation_summary_text", "updated_at"])
    logger.info(
        "Server %s: configuration task started (name=%s ip=%s)",
        server.id,
        server.name,
        server.ip_address,
    )

    if not server.ip_address:
        logger.error("Server %s: no IP available for configuration.", server.id)
        server.status = OdooServer.Status.FAILED
        server.provisioning_log = _append_text(server.provisioning_log, "No server IP available for configuration.")
        server.save(update_fields=["status", "installation_summary", "installation_summary_text", "provisioning_log", "updated_at"])
        _job_done(job_id, ok=False, log="No server IP available for configuration.")
        return

    playbook = os.getenv("ANSIBLE_ODOO_SERVER_PLAYBOOK", "").strip() or _default_odoo_server_playbook()
    if not Path(playbook).exists():
        logger.error("Server %s: bootstrap playbook not found: %s", server.id, playbook)
        server.status = OdooServer.Status.FAILED
        msg = f"Server bootstrap playbook not found: {playbook}"
        server.provisioning_log = _append_text(server.provisioning_log, msg)
        server.save(update_fields=["status", "installation_summary", "installation_summary_text", "provisioning_log", "updated_at"])
        _job_done(job_id, ok=False, log=msg)
        return
    logger.info("Server %s: running Ansible playbook %s", server.id, playbook)

    admin_email = os.getenv("ODOO_ADMIN_EMAIL", "odoo@example.com").strip()
    extra_vars = {
        "odoo_version": server.odoo_version,
        "server_name": server.name,
        "dns_domain": server.dns_domain,
        "website_name": server.dns_domain if server.dns_domain else "_",
        "admin_email": admin_email,
    }

    ws_group = f"odoo.server.{server.id}"
    _broadcast_server(server.id, "Running Ansible playbook — Odoo install takes 5–15 min…", server.status)
    ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
    try:
        ok, log_blob = _run_ansible_playbook(
            playbook,
            str(server.ip_address),
            extra_vars,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key,
            ssh_password=ssh_password,
            on_chunk=lambda line: _broadcast_log_line(ws_group, line),
        )
    finally:
        if tmp_key:
            os.unlink(tmp_key)

    server.provisioning_log = _append_text(server.provisioning_log, f"[ansible server]\n{log_blob}".strip())
    server.status = OdooServer.Status.PROVISIONED if ok else OdooServer.Status.FAILED
    logger.info(
        "Server %s: configuration %s",
        server.id,
        "succeeded" if ok else "failed",
    )
    final_msg = "Server provisioned successfully — ready for instances." if ok else "Server configuration failed."
    server.provisioning_log = _append_text(server.provisioning_log, final_msg)
    summary = {}
    summary_text = ""
    if ok:
        summary, summary_text = _store_installation_summary(
            server,
            ssh_user=ssh_user or "root",
        )
    else:
        server.save(update_fields=["installation_summary", "installation_summary_text", "updated_at"])
    server.save(update_fields=["status", "provisioning_log", "updated_at"])
    _broadcast_server(
        server.id,
        final_msg,
        server.status,
        done=True,
        log=log_blob,
        summary={
            "installation_summary": summary,
            "installation_summary_text": summary_text,
        } if ok else None,
    )
    _job_done(job_id, ok=ok, log=log_blob)

    if ok:
        OdooServerHistory.objects.create(
            server=server,
            odoo_version=server.odoo_version,
            ip_address=server.ip_address,
            dns_domain=server.dns_domain,
            region=server.region,
            size=server.size,
            status=server.status,
            note="Provisioned successfully.",
            deployed_by=server.created_by,
        )

    log_audit(
        server.created_by,
        AuditLog.Action.OTHER,
        None,
        f"Odoo server '{server.name}' configuration finished with status {server.status}.",
        metadata={"server_id": server.id, "odoo_version": server.odoo_version, "ip": str(server.ip_address)},
        organization=server.organization,
    )


@shared_task(bind=True, max_retries=0)
def create_odoo_instance(self, instance_id: int, job_id: int | None = None):
    instance = OdooInstance.objects.select_related(
        "organization",
        "server",
        "server__infrastructure",
        "server__infrastructure__external_server",
    ).get(pk=instance_id)
    server = instance.server

    _job_start(job_id, self.request.id)
    logger.info(
        "Instance %s: creation started (db=%s server=%s ip=%s)",
        instance.id,
        instance.db_name,
        server.id,
        server.ip_address,
    )
    instance.installation_summary = {}
    instance.installation_summary_text = ""
    instance.save(update_fields=["installation_summary", "installation_summary_text", "updated_at"])

    if server.status != OdooServer.Status.PROVISIONED or not server.ip_address:
        logger.error(
            "Instance %s: server not ready for instance creation (status=%s ip=%s)",
            instance.id,
            server.status,
            server.ip_address,
        )
        instance.status = OdooInstance.Status.FAILED
        instance.provisioning_log = "Server is not ready for instance creation."
        instance.save(update_fields=["status", "provisioning_log", "updated_at"])
        _job_done(job_id, ok=False, log="Server is not ready for instance creation.")
        return

    instance.status = OdooInstance.Status.CONFIGURING
    instance.installation_summary = {}
    instance.installation_summary_text = ""
    instance.save(update_fields=["status", "installation_summary", "installation_summary_text", "updated_at"])
    _record_instance_progress(instance, "Starting instance configuration…")
    _broadcast_instance(instance.id, "Starting instance configuration…", instance.status)

    if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
        _run_docker_instance_create(instance, server, job_id, self.request.id)
        return

    direct_playbook = os.getenv("ANSIBLE_ODOO_INSTANCE_DIRECT_PLAYBOOK", "").strip() or _default_odoo_instance_direct_playbook()
    domain_playbook = os.getenv("ANSIBLE_ODOO_INSTANCE_PLAYBOOK", "").strip() or _default_odoo_instance_playbook()
    use_direct = not instance.domain
    playbook = direct_playbook if use_direct else domain_playbook

    if not Path(playbook).exists():
        logger.error("Instance %s: instance playbook not found: %s", instance.id, playbook)
        instance.status = OdooInstance.Status.FAILED
        msg = f"Instance playbook not found: {playbook}"
        instance.provisioning_log = _append_text(instance.provisioning_log, msg)
        instance.save(update_fields=["status", "provisioning_log", "updated_at"])
        _job_done(job_id, ok=False, log=msg)
        return

    extra_vars = {
        "odoo_version": server.odoo_version,
        "db_name": instance.db_name,
        "instance_name": instance.name,
        "http_port": instance.http_port,
        "restart_policy": instance.restart_policy,
    }
    if not use_direct:
        extra_vars["domain"] = instance.domain

    ws_group = f"odoo.instance.{instance.id}"
    ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
    logger.info(
        "Instance %s: running Ansible playbook %s (direct=%s) against %s",
        instance.id,
        playbook,
        use_direct,
        server.ip_address,
    )
    logger.info(
        "Instance %s: playbook target details: server_ip=%s http_port=%s db_name=%s domain=%s ssh_user=%s",
        instance.id,
        server.ip_address,
        instance.http_port,
        instance.db_name,
        instance.domain or "",
        ssh_user,
    )
    playbook_name = playbook.split("/")[-1]
    _record_instance_progress(instance, f"Running {playbook_name} against {server.ip_address}:{instance.http_port}…")
    _broadcast_instance(instance.id, f"Running {playbook_name} against {server.ip_address}:{instance.http_port}…", instance.status)
    try:
        ok, log_blob = _run_ansible_playbook(
            playbook,
            str(server.ip_address),
            extra_vars,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key,
            ssh_password=ssh_password,
            on_chunk=lambda line: _broadcast_log_line(ws_group, line),
        )
    finally:
        if tmp_key:
            os.unlink(tmp_key)

    instance.provisioning_log = f"[ansible instance]\n{log_blob}".strip()
    summary = {}
    summary_text = ""
    if ok:
        instance.status = OdooInstance.Status.RUNNING
        instance.systemd_service = f"odoo-{instance.db_name}"
        instance.nginx_site = "" if use_direct else (instance.domain or f"{instance.name}.local")
        instance.ssl_enabled = False if use_direct else bool(instance.domain)
        summary, summary_text = _store_instance_installation_summary(
            instance,
            server=server,
            playbook=playbook,
            ssh_user=ssh_user or "root",
            use_direct=use_direct,
        )
    else:
        instance.status = OdooInstance.Status.FAILED
    instance.provisioning_log = _append_text(
        instance.provisioning_log,
        "Instance created successfully — ready." if ok else "Instance creation failed.",
    )
    instance.save(
        update_fields=["status", "systemd_service", "nginx_site", "ssl_enabled", "provisioning_log", "updated_at"]
    )
    access_url = f"http://{server.ip_address}:{instance.http_port}" if server.ip_address else ""
    logger.info(
        "Instance %s: creation %s (access=%s)",
        instance.id,
        "succeeded" if ok else "failed",
        access_url or "n/a",
    )
    _broadcast_instance(
        instance.id,
        f"Instance is running — {access_url}" if ok else "Instance creation failed.",
        instance.status,
        done=True,
        log=log_blob,
        summary={
            "installation_summary": summary,
            "installation_summary_text": summary_text,
        } if ok else None,
    )
    _job_done(job_id, ok=ok, log=log_blob)

    if ok:
        OdooInstanceHistory.objects.create(
            instance=instance,
            db_name=instance.db_name,
            domain=instance.domain,
            http_port=instance.http_port,
            odoo_version=server.odoo_version,
            server_ip=server.ip_address,
            systemd_service=instance.systemd_service,
            ssl_enabled=instance.ssl_enabled,
            status=instance.status,
            note="Created successfully.",
            deployed_by=instance.created_by,
        )


@shared_task(bind=True, max_retries=0)
def delete_odoo_instance(self, instance_id: int):
    """Run the direct-IP deletion playbook then mark the instance DELETED."""
    instance = OdooInstance.objects.select_related(
        "organization",
        "server",
        "server__infrastructure",
        "server__infrastructure__external_server",
    ).get(pk=instance_id)
    server = instance.server

    if not server.ip_address:
        # Server IP gone (server deleted); nothing to clean up remotely.
        instance.status = OdooInstance.Status.DELETED
        instance.provisioning_log = _append_text(instance.provisioning_log, "Server IP unavailable; skipped remote cleanup.")
        instance.save(update_fields=["status", "provisioning_log", "updated_at"])
        return

    if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
        _run_docker_instance_delete(instance, server)
        return

    playbook = os.getenv("ANSIBLE_ODOO_INSTANCE_DELETE_PLAYBOOK", "").strip()
    if not playbook:
        instance.status = OdooInstance.Status.DELETED
        instance.provisioning_log = _append_text(instance.provisioning_log, "No delete playbook configured; marked DELETED.")
        instance.save(update_fields=["status", "provisioning_log", "updated_at"])
        return

    ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
    try:
        ok, log_blob = _run_ansible_playbook(
            playbook,
            str(server.ip_address),
            {
                "db_name": instance.db_name,
                "http_port": instance.http_port,
            },
            ssh_user=ssh_user,
            ssh_key_path=ssh_key,
            ssh_password=ssh_password,
        )
    finally:
        if tmp_key:
            os.unlink(tmp_key)
    instance.provisioning_log = _append_text(instance.provisioning_log, f"[ansible delete]\n{log_blob}")
    instance.status = OdooInstance.Status.DELETED
    instance.save(update_fields=["status", "provisioning_log", "updated_at"])


def _tcp_reachable(host: str, port: int, timeout: int = 5) -> bool:
    """Return True if a TCP connection to host:port succeeds within timeout seconds."""
    with suppress(OSError):
        with socket.create_connection((host, port), timeout=timeout):
            return True
    return False


def _odoo_server_ssh_target(server: OdooServer) -> tuple[str | None, int]:
    """Return the SSH host/port for an OdooServer, preferring PYOS external hosts."""
    infra = getattr(server, "infrastructure", None)
    if infra and infra.infra_type == Infrastructure.InfraType.PYOS and infra.external_server:
        ext = infra.external_server
        return str(ext.host), ext.port or 22
    if server.ip_address:
        return str(server.ip_address), 22
    return None, 22


@shared_task
def check_server_connectivity():
    """
    Periodic task: TCP-probe every active OdooServer and ExternalServer.
    Updates is_reachable + last_checked_at without touching other fields.
    """
    from cloud.models import ExternalServer
    from django.utils import timezone

    now = timezone.now()
    logger.info("Periodic reachability sweep started.")

    # --- OdooServer: probe the saved SSH target (PYOS host or direct IP) ---
    servers = OdooServer.objects.select_related(
        "infrastructure",
        "infrastructure__external_server",
    ).filter(is_active=True)

    for server in servers:
        host, port = _odoo_server_ssh_target(server)
        if not host:
            continue
        infra = getattr(server, "infrastructure", None)
        if infra and infra.infra_type == Infrastructure.InfraType.PYOS and infra.external_server:
            reachable = _tcp_reachable(host, port)
            message = f"Host unreachable for {host}:{port}." if not reachable else f"Port reachable at {host}:{port}."
            ext = infra.external_server
            ext.is_verified = reachable
            ext.verification_error = "" if reachable else message
            ext.last_verified_at = now
            ext.save(update_fields=["is_verified", "verification_error", "last_verified_at"])
        else:
            reachable = _tcp_reachable(host, port)
        server.is_reachable = reachable
        server.last_checked_at = now
        update_fields = ["is_reachable", "last_checked_at"]
        if server.ip_address != host:
            server.ip_address = host
            update_fields.append("ip_address")
        server.save(update_fields=update_fields)
        _broadcast_server_snapshot(server)
        logger.info(
            "Reachability check: server %s (%s:%s) is %s",
            server.id,
            host,
            port,
            "connected" if reachable else "disconnected",
        )

    # --- ExternalServer: probe the configured SSH port ---
    ext_servers = ExternalServer.objects.filter(host__isnull=False)

    for ext in ext_servers:
        reachable = _tcp_reachable(str(ext.host), ext.port or 22)
        ext.is_reachable = reachable
        ext.last_checked_at = now
        ext.save(update_fields=["is_reachable", "last_checked_at"])
    logger.info("Periodic reachability sweep finished.")


@shared_task
def check_instance_health():
    """
    Periodic task: HTTP GET /web/health on every RUNNING OdooInstance.
    Updates is_reachable + last_health_check without touching other fields.
    Odoo 16+ exposes GET /web/health → 200 {"status": "pass"}.
    """
    import urllib.request
    import urllib.error

    now = timezone.now()
    instances = OdooInstance.objects.filter(
        status=OdooInstance.Status.RUNNING,
    ).select_related("server").exclude(server__ip_address__isnull=True)

    for instance in instances:
        url = f"http://{instance.server.ip_address}:{instance.http_port}/web/health"
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                reachable = resp.status == 200
        except Exception:
            reachable = False
        instance.is_reachable = reachable
        instance.last_health_check = now
        instance.save(update_fields=["is_reachable", "last_health_check"])


@shared_task(bind=True, max_retries=0)
def rollback_odoo_instance(self, instance_id: int, history_id: int, job_id: int | None = None):
    """
    Re-run instance creation using a historical config snapshot.
    Creates a new OdooInstanceHistory entry on success.
    """
    instance = OdooInstance.objects.select_related(
        "organization",
        "server",
        "server__infrastructure",
        "server__infrastructure__external_server",
    ).get(pk=instance_id)
    snap = OdooInstanceHistory.objects.get(pk=history_id, instance=instance)
    server = instance.server

    _job_start(job_id, self.request.id)

    if not server.ip_address:
        _job_done(job_id, ok=False, log="Server has no IP; rollback aborted.")
        return

    direct_playbook = os.getenv("ANSIBLE_ODOO_INSTANCE_DIRECT_PLAYBOOK", "").strip() or _default_odoo_instance_direct_playbook()
    domain_playbook = os.getenv("ANSIBLE_ODOO_INSTANCE_PLAYBOOK", "").strip() or _default_odoo_instance_playbook()
    use_direct = not snap.domain
    playbook = direct_playbook if use_direct else domain_playbook

    if not Path(playbook).exists():
        _job_done(job_id, ok=False, log=f"Instance playbook not found: {playbook}")
        return

    extra_vars = {
        "odoo_version": snap.odoo_version,
        "db_name": snap.db_name,
        "instance_name": instance.name,
        "http_port": snap.http_port,
        "restart_policy": instance.restart_policy,
    }
    if not use_direct:
        extra_vars["domain"] = snap.domain

    instance.status = OdooInstance.Status.CONFIGURING
    instance.save(update_fields=["status", "updated_at"])
    _broadcast_instance(instance.id, f"Rolling back to snapshot #{snap.pk}…", instance.status)

    ws_group = f"odoo.instance.{instance.id}"
    ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
    try:
        ok, log_blob = _run_ansible_playbook(
            playbook,
            str(server.ip_address),
            extra_vars,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key,
            ssh_password=ssh_password,
            on_chunk=lambda line: _broadcast_log_line(ws_group, line),
        )
    finally:
        if tmp_key:
            os.unlink(tmp_key)

    instance.provisioning_log = _append_text(instance.provisioning_log, f"[rollback to #{snap.pk}]\n{log_blob}")
    instance.status = OdooInstance.Status.RUNNING if ok else OdooInstance.Status.FAILED
    instance.save(update_fields=["status", "provisioning_log", "updated_at"])
    _broadcast_instance(
        instance.id,
        f"Rollback to #{snap.pk} succeeded." if ok else f"Rollback to #{snap.pk} failed.",
        instance.status,
        done=True,
        log=log_blob,
    )
    _job_done(job_id, ok=ok, log=log_blob)

    if ok:
        OdooInstanceHistory.objects.create(
            instance=instance,
            db_name=snap.db_name,
            domain=snap.domain,
            http_port=snap.http_port,
            odoo_version=snap.odoo_version,
            server_ip=server.ip_address,
            systemd_service=instance.systemd_service,
            ssl_enabled=snap.ssl_enabled,
            status=instance.status,
            note=f"Rolled back from snapshot #{snap.pk}.",
            deployed_by=instance.created_by,
        )


# ---------------------------------------------------------------------------
# Docker deployment helpers + tasks
# ---------------------------------------------------------------------------

def _run_docker_instance_create(instance: OdooInstance, server: OdooServer, job_id, celery_task_id):
    """
    Internal: run the Docker Odoo instance creation playbook and update model state.
    Called from create_odoo_instance when server.deployment_mode == DOCKER.
    """
    _job_start(job_id, celery_task_id)
    instance.installation_summary = {}
    instance.installation_summary_text = ""
    instance.save(update_fields=["installation_summary", "installation_summary_text", "updated_at"])
    _record_instance_progress(instance, "Starting Docker instance creation…")

    playbook = os.getenv("ANSIBLE_DOCKER_INSTANCE_PLAYBOOK", "").strip() or _default_docker_instance_playbook()
    if not Path(playbook).exists():
        logger.error("Instance %s: Docker instance playbook not found: %s", instance.id, playbook)
        instance.status = OdooInstance.Status.FAILED
        msg = f"Docker instance playbook not found: {playbook}"
        instance.provisioning_log = _append_text(instance.provisioning_log, msg)
        instance.save(update_fields=["status", "provisioning_log", "updated_at"])
        _job_done(job_id, ok=False, log=msg)
        return

    client_name = instance.db_name.replace("_", "-")
    container_name = f"odoo-{client_name}"
    extra_vars = {
        "client_name": client_name,
        "domain": instance.domain,
        "db_name": instance.db_name,
        "odoo_version": server.odoo_version,
        "postgres_password": server.docker_postgres_password,
        "restart_policy": "unless-stopped",
        "container_name": container_name,
    }

    ws_group = f"odoo.instance.{instance.id}"
    ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
    logger.info(
        "Instance %s: Docker playbook target details: server_ip=%s domain=%s db_name=%s ssh_user=%s",
        instance.id,
        server.ip_address,
        instance.domain or "",
        instance.db_name,
        ssh_user,
    )
    playbook_name = playbook.split("/")[-1]
    _record_instance_progress(instance, f"Running {playbook_name} for {instance.domain or instance.db_name}…")
    _broadcast_instance(instance.id, "Running Docker instance creation playbook…", instance.status)
    try:
        ok, log_blob = _run_ansible_playbook(
            playbook,
            str(server.ip_address),
            extra_vars,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key,
            ssh_password=ssh_password,
            on_chunk=lambda line: _broadcast_log_line(ws_group, line),
        )
    finally:
        if tmp_key:
            os.unlink(tmp_key)

    instance.provisioning_log = f"[docker create]\n{log_blob}".strip()
    summary = {}
    summary_text = ""
    if ok:
        instance.container_name = container_name
        instance.status = OdooInstance.Status.RUNNING
        instance.ssl_enabled = True
        summary, summary_text = _store_instance_installation_summary(
            instance,
            server=server,
            playbook=playbook,
            ssh_user=ssh_user or "root",
            use_direct=False,
        )
    else:
        instance.status = OdooInstance.Status.FAILED
    instance.provisioning_log = _append_text(
        instance.provisioning_log,
        "Docker instance created successfully — ready." if ok else "Docker instance creation failed.",
    )
    instance.save(update_fields=["container_name", "status", "ssl_enabled", "provisioning_log", "updated_at"])

    access_url = f"https://{instance.domain}" if instance.domain else ""
    _broadcast_instance(
        instance.id,
        f"Docker instance running — {access_url}" if ok else "Docker instance creation failed.",
        instance.status,
        done=True,
        log=log_blob,
        summary={
            "installation_summary": summary,
            "installation_summary_text": summary_text,
        } if ok else None,
    )
    _job_done(job_id, ok=ok, log=log_blob)

    if ok:
        OdooInstanceHistory.objects.create(
            instance=instance,
            db_name=instance.db_name,
            domain=instance.domain,
            http_port=8069,
            odoo_version=server.odoo_version,
            server_ip=server.ip_address,
            systemd_service="",
            ssl_enabled=True,
            status=instance.status,
            note="Docker container created successfully.",
            deployed_by=instance.created_by,
        )


def _run_docker_instance_delete(instance: OdooInstance, server: OdooServer):
    """
    Internal: run the Docker Odoo instance deletion playbook and mark the instance DELETED.
    Called from delete_odoo_instance when server.deployment_mode == DOCKER.
    """
    playbook = os.getenv("ANSIBLE_DOCKER_INSTANCE_DELETE_PLAYBOOK", "").strip()
    if not playbook:
        instance.status = OdooInstance.Status.DELETED
        instance.provisioning_log = _append_text(
            instance.provisioning_log, "ANSIBLE_DOCKER_INSTANCE_DELETE_PLAYBOOK not set; marked DELETED."
        )
        instance.save(update_fields=["status", "provisioning_log", "updated_at"])
        return

    client_name = instance.db_name.replace("_", "-")
    extra_vars = {
        "client_name": client_name,
        "db_name": instance.db_name,
        "remove_filestore": "false",
    }

    ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
    try:
        ok, log_blob = _run_ansible_playbook(
            playbook,
            str(server.ip_address),
            extra_vars,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key,
            ssh_password=ssh_password,
        )
    finally:
        if tmp_key:
            os.unlink(tmp_key)

    instance.provisioning_log = _append_text(instance.provisioning_log, f"[docker delete]\n{log_blob}")
    instance.status = OdooInstance.Status.DELETED
    instance.save(update_fields=["status", "provisioning_log", "updated_at"])


@shared_task(bind=True, max_retries=0)
def configure_docker_host(self, server_id: int, job_id: int | None = None):
    """
    Install Docker on the host, create odoo-network, and start Traefik + PostgreSQL.
    Runs the setup_docker_host.yml Ansible playbook.
    """
    server = OdooServer.objects.select_related(
        "organization",
        "infrastructure",
        "infrastructure__external_server",
    ).get(pk=server_id)

    _job_start(job_id, self.request.id)

    if not server.ip_address:
        server.status = OdooServer.Status.FAILED
        server.provisioning_log = _append_text(server.provisioning_log, "No server IP available for Docker host setup.")
        server.save(update_fields=["status", "provisioning_log", "updated_at"])
        _job_done(job_id, ok=False, log="No server IP available for Docker host setup.")
        return

    playbook = os.getenv("ANSIBLE_DOCKER_HOST_PLAYBOOK", "").strip()
    if not playbook:
        server.status = OdooServer.Status.PROVISIONED
        msg = "ANSIBLE_DOCKER_HOST_PLAYBOOK not set. Marked PROVISIONED without Docker setup."
        server.provisioning_log = _append_text(server.provisioning_log, msg)
        server.save(update_fields=["status", "provisioning_log", "updated_at"])
        _job_done(job_id, ok=True, log=msg)
        return

    acme_email = os.getenv("ODOO_ADMIN_EMAIL", "odoo@example.com").strip()
    pg_password = server.docker_postgres_password or os.getenv("DOCKER_POSTGRES_PASSWORD", "odoo_secret")
    if not server.docker_postgres_password:
        server.docker_postgres_password = pg_password
        server.save(update_fields=["docker_postgres_password"])

    extra_vars = {
        "acme_email": acme_email,
        "postgres_password": pg_password,
    }

    ws_group = f"odoo.server.{server.id}"
    _broadcast_server(server.id, "Running Docker host setup playbook…", server.status)
    ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
    try:
        ok, log_blob = _run_ansible_playbook(
            playbook,
            str(server.ip_address),
            extra_vars,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key,
            ssh_password=ssh_password,
            on_chunk=lambda line: _broadcast_log_line(ws_group, line),
        )
    finally:
        if tmp_key:
            os.unlink(tmp_key)

    server.provisioning_log = _append_text(server.provisioning_log, f"[docker host setup]\n{log_blob}".strip())
    server.status = OdooServer.Status.PROVISIONED if ok else OdooServer.Status.FAILED
    final_msg = "Docker host ready — Traefik + PostgreSQL running." if ok else "Docker host setup failed."
    server.provisioning_log = _append_text(server.provisioning_log, final_msg)
    server.save(update_fields=["status", "provisioning_log", "updated_at"])
    _broadcast_server(
        server.id,
        final_msg,
        server.status,
        done=True,
        log=log_blob,
    )
    _job_done(job_id, ok=ok, log=log_blob)

    if ok:
        OdooServerHistory.objects.create(
            server=server,
            odoo_version=server.odoo_version,
            ip_address=server.ip_address,
            dns_domain=server.dns_domain,
            region=server.region,
            size=server.size,
            status=server.status,
            note="Docker host provisioned (Traefik + PostgreSQL).",
            deployed_by=server.created_by,
        )

    log_audit(
        server.created_by,
        AuditLog.Action.OTHER,
        None,
        f"Docker host '{server.name}' setup finished with status {server.status}.",
        metadata={"server_id": server.id, "ip": str(server.ip_address)},
        organization=server.organization,
    )


@shared_task(bind=True, max_retries=0)
def deploy_server_ssh_key(self, ssh_key_id: int):
    """
    Append a ServerSSHKey's public_key to /root/.ssh/authorized_keys on the target
    server using Paramiko (no Ansible required — avoids ansible-playbook dependency).
    """
    from deployments.models import ServerSSHKey
    from cloud.models import SystemSSHKey

    key_obj = ServerSSHKey.objects.select_related(
        "server", "server__infrastructure", "server__infrastructure__external_server"
    ).get(pk=ssh_key_id)
    server = key_obj.server

    if not server.ip_address:
        logger.error("deploy_server_ssh_key: server %s has no IP — cannot deploy key.", server.pk)
        return

    pub_key = key_obj.public_key.strip()

    # Resolve SSH credentials (same logic as Ansible creds but via Paramiko)
    ssh_user = None
    pkey = None
    password = None
    tmp_key_path = None

    try:
        infra = server.infrastructure
        if infra and infra.infra_type == Infrastructure.InfraType.PYOS and infra.external_server:
            ext = infra.external_server
            ssh_user = ext.username or "root"
            if ext.auth_type == "DAFEAPP_KEY":
                system_key = SystemSSHKey.get_or_create_keypair()
                private_key_str = system_key.get_private_key()
                if private_key_str:
                    import io
                    pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(private_key_str))
            else:
                from cloud.encryption import FieldEncryptor
                password = FieldEncryptor.decrypt(ext.encrypted_password)
        elif infra and infra.infra_type == Infrastructure.InfraType.MANAGED:
            system_key = SystemSSHKey.get_or_create_keypair()
            private_key_str = system_key.get_private_key()
            if private_key_str:
                import io
                pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(private_key_str))
            ssh_user = os.getenv("ANSIBLE_SSH_USER", "root").strip()
    except Exception:
        logger.warning("deploy_server_ssh_key: could not resolve SSH creds for server %s", server.pk, exc_info=True)

    if not ssh_user:
        ssh_user = "root"

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ok = False
    try:
        connect_kwargs = {
            "hostname": str(server.ip_address),
            "username": ssh_user,
            "timeout": 15,
            "banner_timeout": 15,
        }
        if pkey:
            connect_kwargs["pkey"] = pkey
        elif password:
            connect_kwargs["password"] = password
        else:
            logger.error("deploy_server_ssh_key: no credentials available for server %s", server.pk)
            return

        client.connect(**connect_kwargs)

        # Idempotent: add the key only if not already present
        safe_key = pub_key.replace("'", "'\\''")
        cmd = (
            "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
            f"grep -qxF '{safe_key}' /root/.ssh/authorized_keys 2>/dev/null || "
            f"echo '{safe_key}' >> /root/.ssh/authorized_keys && "
            "chmod 600 /root/.ssh/authorized_keys"
        )
        _, stdout, stderr = client.exec_command(cmd, timeout=15)
        exit_status = stdout.channel.recv_exit_status()
        ok = exit_status == 0
        if not ok:
            logger.warning(
                "deploy_server_ssh_key #%s: exit=%s stderr=%s",
                ssh_key_id, exit_status, stderr.read().decode()
            )
    except Exception as exc:
        logger.error("deploy_server_ssh_key #%s failed: %s", ssh_key_id, exc)
        ok = False
    finally:
        client.close()
        if tmp_key_path:
            try:
                os.unlink(tmp_key_path)
            except OSError:
                pass

    key_obj.deployed = ok
    key_obj.save(update_fields=["deployed"])
    logger.info("deploy_server_ssh_key #%s: ok=%s", ssh_key_id, ok)
