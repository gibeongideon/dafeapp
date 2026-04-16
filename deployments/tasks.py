import json
import logging
import os
import re
import shlex
import socket
import subprocess
import tempfile
import time
import traceback
from contextlib import suppress
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

import paramiko
from asgiref.sync import async_to_sync
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.cache import cache
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from audit.models import AuditLog
from cloud.providers import get_provider
from core.utils import log_audit
from dns.models import DomainAssignment, DnsRecord, DnsZone, normalize_domain_name
from dns.services.factory import get_dns_provider_service
from deployments.domain_utils import (
    build_platform_domain_label,
    is_platform_domain_label_valid,
    normalize_platform_domain_label,
    platform_base_domain,
    platform_dns_default_proxied,
    platform_dns_is_configured,
    platform_dns_provider_service,
    platform_domain_for_label,
    slugify_branch,
)
from deployments.models import (
    DeploymentJob,
    EnterpriseSource,
    GitRepositoryCredential,
    Infrastructure,
    Instance,
    OdooInstance,
    OdooInstanceGitRepo,
    OdooInstanceHistory,
    OdooServer,
    OdooServerHistory,
    StagingEnvironment,
    TerraformRun,
)

logger = logging.getLogger(__name__)


def _channel_safe(value):
    """Convert datetimes and other Django JSON values into msgpack-safe primitives."""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


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
            _channel_safe({"type": "server.update", "payload": payload}),
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
            _channel_safe({
                "type": "server.update",
                "payload": {
                    "type": "snapshot",
                    "server": OdooServerSerializer(server).data,
                },
            }),
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
            _channel_safe({"type": "instance.update", "payload": payload}),
        )
    except Exception:
        logger.warning("Instance broadcast skipped for instance %s", instance_id, exc_info=True)


def _broadcast_instance_removed(instance_id: int, server_id: int):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    payload = {
        "type": "removed",
        "instance_id": instance_id,
        "server_id": server_id,
        "reason": "deleted",
    }
    try:
        async_to_sync(channel_layer.group_send)(
            f"odoo.instance.{instance_id}",
            _channel_safe({"type": "instance.update", "payload": payload}),
        )
    except Exception:
        logger.warning("Instance removal broadcast skipped for instance %s", instance_id, exc_info=True)


def _broadcast_repo_event(instance_id: int, payload: dict):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            f"odoo.instance.{instance_id}",
            {"type": "instance.update", "payload": payload},
        )
    except Exception:
        logger.warning("Repo broadcast skipped for instance %s", instance_id, exc_info=True)


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
            _channel_safe({"type": "deployment.update", "payload": payload}),
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
            _channel_safe({"type": "log.line", "payload": {"type": "log_line", "line": line.rstrip()}}),
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


def _repo_lock_key(instance_id: int) -> str:
    return f"deployments:instance:{instance_id}:repo-lock"


def _acquire_repo_lock(instance_id: int, ttl: int = 1800) -> str:
    token = f"{instance_id}:{time.time()}"
    if not cache.add(_repo_lock_key(instance_id), token, ttl):
        raise RuntimeError("Another addon sync is already running for this instance.")
    return token


def _release_repo_lock(instance_id: int, token: str):
    key = _repo_lock_key(instance_id)
    if cache.get(key) == token:
        cache.delete(key)


def _truncate_text(value: str, limit: int = 12000) -> str:
    value = value or ""
    return value[-limit:] if len(value) > limit else value


def _append_repo_log(repo: OdooInstanceGitRepo, message: str, *, reset: bool = False):
    repo.last_sync_log = message.strip() if reset else _append_text(repo.last_sync_log, message)
    repo.last_sync_log = _truncate_text(repo.last_sync_log, limit=16000)


def _set_repo_status(
    repo: OdooInstanceGitRepo,
    *,
    status: str | None = None,
    last_error: str | None = None,
    append_log: str | None = None,
    reset_log: bool = False,
    started: bool = False,
    finished: bool = False,
    save: bool = True,
):
    update_fields = ["updated_at"]
    if status is not None:
        repo.status = status
        update_fields.append("status")
    if last_error is not None:
        repo.last_error = last_error
        update_fields.append("last_error")
    if append_log:
        _append_repo_log(repo, append_log, reset=reset_log)
        update_fields.append("last_sync_log")
    if started:
        repo.last_sync_started_at = timezone.now()
        update_fields.append("last_sync_started_at")
    if finished:
        repo.last_sync_finished_at = timezone.now()
        update_fields.append("last_sync_finished_at")
    if save:
        repo.save(update_fields=list(dict.fromkeys(update_fields)))
    _broadcast_repo_event(
        repo.instance_id,
        {
            "type": "repo.update",
            "repo_id": repo.id,
            "status": repo.status,
            "last_error": repo.last_error,
            "last_sync_finished_at": repo.last_sync_finished_at.isoformat() if repo.last_sync_finished_at else "",
        },
    )


def _repo_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip().lower()).strip("-._")
    return slug or f"repo-{int(time.time())}"


def _enterprise_host_local_path(instance: OdooInstance) -> str:
    runtime = _instance_runtime_context(instance)
    if instance.enterprise_remote_path:
        return instance.enterprise_remote_path
    addons_root = runtime["addons_root_path"].rstrip("/")
    if runtime["mode"] == "bare_direct":
        return f"{str(Path(addons_root).parent).rstrip('/')}/enterprise"
    return f"{addons_root}/enterprise"


def _enterprise_config_path(instance: OdooInstance) -> str:
    runtime = _instance_runtime_context(instance)
    if runtime["mode"] == "docker":
        # Enterprise addons are bind-mounted at /mnt/extra-addons inside the container.
        return "/mnt/extra-addons"
    return _enterprise_host_local_path(instance)


def _instance_runtime_context(instance: OdooInstance) -> dict:
    summary = instance.installation_summary or {}
    server = instance.server
    db_name = instance.db_name
    if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
        client_name = db_name.replace("_", "-")
        addons_root = instance.addons_root_path or f"/data/odoo/{client_name}/addons"
        container = instance.container_name or f"odoo-{client_name}"
        return {
            "mode": "docker",
            "addons_root_path": addons_root,
            "config_file": summary.get("config_file") or f"/opt/odoo-docker/instances/{client_name}.conf",
            "core_addons_path": "/usr/lib/python3/dist-packages/odoo/addons",
            "container_addons_root": "/var/lib/odoo/addons",
            "restart_command": f"docker restart {shlex.quote(container)}",
            "stop_command": f"docker stop {shlex.quote(container)}",
            "container_name": container,
        }

    if summary.get("core_addons_dir"):
        addons_root = instance.addons_root_path or summary.get("custom_addons_dir") or f"/odoo/instances/{db_name}/addons/custom"
        svc = shlex.quote(instance.systemd_service or f"odoo-{db_name}")
        return {
            "mode": "bare_direct",
            "addons_root_path": addons_root,
            "manual_addons_root": summary.get("custom_addons_dir") or addons_root,
            "config_file": summary.get("config_file") or f"/etc/odoo-{db_name}.conf",
            "core_addons_path": summary.get("core_addons_dir") or f"/odoo/instances/{db_name}/addons/core",
            "odoo_bin": f"{summary.get('venv_dir') or f'/odoo/instances/{db_name}/venv'}/bin/python {summary.get('source_dir') or '/odoo/odoo-server'}/odoo-bin",
            "restart_command": f"systemctl restart {svc}",
            "stop_command": f"systemctl stop {svc}",
        }

    instance_dir = summary.get("instance_dir") or f"/opt/odoo{server.odoo_version}/instances/{db_name}"
    odoo_home = f"/opt/odoo{server.odoo_version}"
    addons_root = instance.addons_root_path or f"{instance_dir}/addons"
    svc = shlex.quote(instance.systemd_service or f"odoo-{db_name}")
    return {
        "mode": "bare_domain",
        "addons_root_path": addons_root,
        "manual_addons_root": "",
        "config_file": summary.get("config_file") or f"{instance_dir}/odoo.conf",
        "core_addons_path": summary.get("addons_dir") or f"{odoo_home}/src/odoo/addons",
        "odoo_bin": f"{summary.get('venv_dir') or f'{odoo_home}/venv'}/bin/python {summary.get('source_dir') or f'{odoo_home}/src/odoo'}/odoo-bin",
        "restart_command": f"systemctl restart {svc}",
        "stop_command": f"systemctl stop {svc}",
    }


def _repo_host_local_path(instance: OdooInstance, repo: OdooInstanceGitRepo) -> str:
    runtime = _instance_runtime_context(instance)
    if repo.local_path:
        return repo.local_path
    return f"{runtime['addons_root_path'].rstrip('/')}/{_repo_slug(repo.repo_name)}"


def _repo_config_path(instance: OdooInstance, repo: OdooInstanceGitRepo) -> str:
    runtime = _instance_runtime_context(instance)
    host_path = _repo_host_local_path(instance, repo)
    if runtime["mode"] == "docker":
        prefix = runtime["addons_root_path"].rstrip("/")
        suffix = host_path[len(prefix):].lstrip("/") if host_path.startswith(prefix) else _repo_slug(repo.repo_name)
        return f"{runtime['container_addons_root'].rstrip('/')}/{suffix}"
    return host_path


def _compute_addons_path(instance: OdooInstance) -> tuple[str, str]:
    runtime = _instance_runtime_context(instance)
    repos = list(instance.git_repos.filter(is_enabled=True).order_by("display_order", "repo_name", "id"))
    repo_paths = [_repo_config_path(instance, repo) for repo in repos]
    enterprise_path = _enterprise_config_path(instance) if instance.enterprise_enabled else ""

    if repo_paths:
        parts = [runtime["core_addons_path"]]
        if enterprise_path:
            parts.append(enterprise_path)
        parts.extend(repo_paths)
    elif runtime["mode"] == "docker":
        parts = [runtime["container_addons_root"], runtime["core_addons_path"]]
        if enterprise_path:
            parts.insert(1, enterprise_path)
    elif runtime.get("manual_addons_root"):
        parts = [runtime["core_addons_path"], runtime["manual_addons_root"]]
        if enterprise_path:
            parts.insert(1, enterprise_path)
    else:
        parts = [runtime["core_addons_path"]]
        if enterprise_path:
            parts.append(enterprise_path)
    return runtime["addons_root_path"], ",".join(parts)


def _repo_clone_url(repo: OdooInstanceGitRepo) -> str:
    if repo.auth_type == OdooInstanceGitRepo.AuthType.PUBLIC or not repo.credential_id:
        return repo.git_url

    credential = repo.credential
    if repo.auth_type in (
        OdooInstanceGitRepo.AuthType.GITHUB_OAUTH,
        OdooInstanceGitRepo.AuthType.TOKEN,
    ):
        token = credential.access_token.strip()
        if not token:
            raise ValueError("The selected Git credential has no access token.")
        parsed = urlparse(repo.git_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Token-based auth currently requires an HTTPS Git URL.")
        username = credential.git_username.strip() or "oauth2"
        netloc = f"{quote(username, safe='')}:{quote(token, safe='')}@{parsed.netloc}"
        return urlunparse(parsed._replace(netloc=netloc))

    return repo.git_url


def _update_repo_paths(instance: OdooInstance, repo: OdooInstanceGitRepo):
    addons_root, addons_path = _compute_addons_path(instance)
    repo.local_path = _repo_host_local_path(instance, repo)
    repo.save(update_fields=["local_path", "updated_at"])
    instance.addons_root_path = addons_root
    instance.addons_path_cache = addons_path
    instance.save(update_fields=["addons_root_path", "addons_path_cache", "updated_at"])


def _connect_ssh_client(server: OdooServer):
    host, port = _odoo_server_ssh_target(server)
    if not host:
        raise RuntimeError("Server has no reachable SSH target.")

    ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = {
        "hostname": host,
        "port": port,
        "username": ssh_user or "root",
        "timeout": 20,
        "banner_timeout": 20,
    }
    if ssh_key:
        kwargs["key_filename"] = ssh_key
    elif ssh_password:
        kwargs["password"] = ssh_password
    else:
        raise RuntimeError("No SSH credentials available for this server.")
    client.connect(**kwargs)
    return client, tmp_key


def _ssh_run(
    server: OdooServer,
    command: str,
    *,
    timeout: int = 1800,
    on_line=None,
) -> tuple[int, str]:
    client = None
    tmp_key = None
    try:
        client, tmp_key = _connect_ssh_client(server)
        transport = client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport is unavailable.")
        channel = transport.open_session()
        channel.get_pty()
        channel.settimeout(timeout)
        channel.exec_command(f"bash -lc {shlex.quote(command)}")

        output_chunks: list[str] = []
        while True:
            made_progress = False
            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="replace")
                output_chunks.append(chunk)
                for line in chunk.splitlines():
                    if on_line and line.strip():
                        on_line(line)
                made_progress = True
            if channel.recv_stderr_ready():
                chunk = channel.recv_stderr(4096).decode(errors="replace")
                output_chunks.append(chunk)
                for line in chunk.splitlines():
                    if on_line and line.strip():
                        on_line(line)
                made_progress = True
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if not made_progress:
                time.sleep(0.1)

        return channel.recv_exit_status(), "".join(output_chunks).strip()
    finally:
        if client is not None:
            client.close()
        if tmp_key:
            with suppress(OSError):
                os.unlink(tmp_key)


def _write_remote_config_addons_path(server: OdooServer, config_file: str, addons_path: str, *, on_line=None) -> str:
    script = f"""
python3 - <<'PY'
from pathlib import Path
config = Path({config_file!r})
if not config.exists():
    raise SystemExit(f"Config file not found: {{config}}")
line = "addons_path = {addons_path}"
lines = config.read_text().splitlines()
updated = []
replaced = False
for existing in lines:
    if existing.strip().startswith("addons_path"):
        updated.append(line)
        replaced = True
    else:
        updated.append(existing)
if not replaced:
    updated.append(line)
config.write_text("\\n".join(updated) + "\\n")
print(line)
PY
"""
    code, output = _ssh_run(server, script, on_line=on_line)
    if code != 0:
        raise RuntimeError(output or "Failed to rewrite the instance config addons_path.")
    return output


def _instance_refresh_module_command(instance: OdooInstance, modules: list[str] | None = None) -> str:
    runtime = _instance_runtime_context(instance)
    modules = [m for m in (modules or []) if m]
    if runtime["mode"] == "docker":
        container = shlex.quote(runtime["container_name"])
        if modules:
            return (
                f"docker exec {container} odoo -c /etc/odoo/odoo.conf -d {shlex.quote(instance.db_name)} "
                f"-u {shlex.quote(','.join(modules))} --stop-after-init --no-http"
            )
        shell_payload = "env['ir.module.module'].update_list(); env.cr.commit(); print('module list refreshed')"
        return (
            f"printf '%s\n' {shlex.quote(shell_payload)} | "
            f"docker exec -i {container} odoo shell -c /etc/odoo/odoo.conf -d {shlex.quote(instance.db_name)} --no-http"
        )

    odoo_bin = runtime["odoo_bin"]
    config_file = runtime["config_file"]
    service_user = runtime.get("service_user") or "odoo"
    if modules:
        command = (
            f"{odoo_bin} -c {shlex.quote(config_file)} -d {shlex.quote(instance.db_name)} "
            f"-u {shlex.quote(','.join(modules))} --stop-after-init --no-http"
        )
        return f"su -s /bin/bash {shlex.quote(service_user)} -c {shlex.quote(command)}"
    shell_payload = "env['ir.module.module'].update_list(); env.cr.commit(); print('module list refreshed')"
    command = (
        f"printf '%s\n' {shlex.quote(shell_payload)} | "
        f"{odoo_bin} shell -c {shlex.quote(config_file)} -d {shlex.quote(instance.db_name)} --no-http"
    )
    return f"su -s /bin/bash {shlex.quote(service_user)} -c {shlex.quote(command)}"


def _detect_repo_modules(server: OdooServer, repo_path: str) -> list[str]:
    script = f"""
python3 - <<'PY'
import json
from pathlib import Path
repo = Path({repo_path!r})
mods = set()
for manifest in repo.glob("*/__manifest__.py"):
    mods.add(manifest.parent.name)
for manifest in repo.glob("*/*/__manifest__.py"):
    mods.add(manifest.parent.name)
for manifest in repo.glob("*/__openerp__.py"):
    mods.add(manifest.parent.name)
for manifest in repo.glob("*/*/__openerp__.py"):
    mods.add(manifest.parent.name)
print(json.dumps(sorted(mods)))
PY
"""
    code, output = _ssh_run(server, script)
    if code != 0:
        return []
    try:
        return json.loads(output or "[]")
    except json.JSONDecodeError:
        return []


def _detect_changed_modules(server: OdooServer, repo_path: str, previous_commit: str, current_commit: str) -> list[str]:
    if not previous_commit or not current_commit or previous_commit == current_commit:
        return []
    script = f"""
python3 - <<'PY'
import json
import subprocess
from pathlib import Path
repo = Path({repo_path!r})
previous = {previous_commit!r}
current = {current_commit!r}
changed = subprocess.check_output(
    ["git", "-C", str(repo), "diff", "--name-only", previous, current],
    text=True,
).splitlines()
mods = set()
for rel in changed:
    parts = Path(rel).parts
    for depth in (1, 2):
        if len(parts) < depth:
            continue
        candidate = repo.joinpath(*parts[:depth])
        if (candidate / "__manifest__.py").exists() or (candidate / "__openerp__.py").exists():
            mods.add(candidate.name)
            break
print(json.dumps(sorted(mods)))
PY
"""
    code, output = _ssh_run(server, script)
    if code != 0:
        return []
    try:
        return json.loads(output or "[]")
    except json.JSONDecodeError:
        return []


def _detect_repo_requirement_files(instance: OdooInstance, repo: OdooInstanceGitRepo) -> list[str]:
    runtime = _instance_runtime_context(instance)
    repo_path = _repo_config_path(instance, repo) if runtime["mode"] == "docker" else repo.local_path
    script = f"""
python3 - <<'PY'
import json
from pathlib import Path
repo = Path({repo_path!r})
if not repo.exists():
    print("[]")
    raise SystemExit(0)
files = []
for path in repo.rglob("requirements*.txt"):
    parts = set(path.parts)
    if ".git" in parts or ".venv" in parts or "node_modules" in parts:
        continue
    if path.is_file():
        files.append(str(path))
print(json.dumps(sorted(set(files))))
PY
"""
    code, output = _ssh_run(instance.server, script)
    if code != 0:
        return []
    try:
        return json.loads(output or "[]")
    except json.JSONDecodeError:
        return []


def _install_repo_python_requirements(repo: OdooInstanceGitRepo, *, on_line=None) -> str:
    instance = repo.instance
    runtime = _instance_runtime_context(instance)
    requirement_files = _detect_repo_requirement_files(instance, repo)
    if not requirement_files:
        return "No requirements files found. Skipping Python dependency install."

    commands: list[str] = []
    if runtime["mode"] == "docker":
        container = shlex.quote(runtime["container_name"])
        for requirement_file in requirement_files:
            commands.append(
                f"docker exec {container} python3 -m pip install -r {shlex.quote(requirement_file)}"
            )
    else:
        summary = instance.installation_summary or {}
        default_venv = (
            f"/odoo/instances/{instance.db_name}/venv"
            if runtime["mode"] == "bare_direct"
            else f"/opt/odoo{instance.server.odoo_version}/venv"
        )
        pip_bin = f"{summary.get('venv_dir') or default_venv}/bin/pip"
        for requirement_file in requirement_files:
            commands.append(f"{shlex.quote(pip_bin)} install -r {shlex.quote(requirement_file)}")

    log_lines = [f"Installing Python requirements from {len(requirement_files)} file(s)…", *requirement_files]
    code, output = _ssh_run(instance.server, " && ".join(commands), on_line=on_line)
    if code != 0:
        raise RuntimeError(output or "Failed to install Python requirements for the updated repository.")
    return _append_text("\n".join(log_lines), output)


def _restart_and_refresh_instance_addons(
    instance: OdooInstance,
    *,
    modules: list[str] | None = None,
    allow_refresh_failure: bool = False,
    on_line=None,
) -> str:
    server = instance.server
    runtime = _instance_runtime_context(instance)
    restart_code, restart_output = _ssh_run(server, runtime["restart_command"], on_line=on_line)
    if restart_code != 0:
        raise RuntimeError(restart_output or "Failed to restart Odoo after syncing addons.")

    refresh_commands = [_instance_refresh_module_command(instance, modules=None)]
    if modules:
        refresh_commands.append(_instance_refresh_module_command(instance, modules=modules))
    code, output = _ssh_run(server, " && ".join(refresh_commands), on_line=on_line)
    if code != 0:
        if not allow_refresh_failure:
            raise RuntimeError(output or "Failed to refresh the Odoo module registry after restarting.")
        warning = (
            "Odoo restarted and addons_path was updated, but the module registry refresh did not complete yet. "
            "You can retry the refresh after the database becomes available."
        )
        return _append_text(restart_output, _append_text(output, warning))
    return _append_text(restart_output, output)


def _upgrade_all_instance_modules_once(
    instance: OdooInstance,
    *,
    on_line=None,
) -> str:
    """
    Run a one-shot `-u all --stop-after-init --no-http` using the instance config.
    This is stronger than update_list() and helps rebuild assets/action registries
    after addons_path changes such as Enterprise activation.
    """
    runtime = _instance_runtime_context(instance)
    db = shlex.quote(instance.db_name)
    service_user = runtime.get("service_user") or "odoo"

    if runtime["mode"] == "docker":
        container = shlex.quote(runtime.get("container_name", ""))
        update_cmd = (
            f"docker exec {container} odoo -c /etc/odoo/odoo.conf -d {db} "
            f"-u all --stop-after-init --no-http"
        )
    else:
        odoo_bin = runtime.get("odoo_bin", "odoo-bin")
        config = shlex.quote(runtime.get("config_file", ""))
        inner = f"{odoo_bin} -c {config} -d {db} -u all --stop-after-init --no-http"
        update_cmd = f"su -s /bin/bash {shlex.quote(service_user)} -c {shlex.quote(inner)}"

    code, output = _ssh_run(instance.server, update_cmd, on_line=on_line, timeout=3600)
    if code != 0:
        raise RuntimeError(output or "Failed to upgrade modules after Enterprise activation.")
    return output


def _prepare_remote_git_key(server: OdooServer, repo: OdooInstanceGitRepo) -> tuple[str, str]:
    credential = repo.credential
    if not credential:
        raise ValueError("This repository does not have an SSH credential attached.")
    private_key = credential.ssh_private_key.strip()
    if not private_key:
        raise ValueError("The selected SSH credential has no private key.")

    remote_path = f"/tmp/dafeapp_repo_key_{repo.id}_{int(time.time())}"
    client = None
    tmp_key = None
    try:
        client, tmp_key = _connect_ssh_client(server)
        sftp = client.open_sftp()
        try:
            with sftp.open(remote_path, "w") as remote_file:
                remote_file.write(private_key)
            sftp.chmod(remote_path, 0o600)
        finally:
            sftp.close()
    finally:
        if client is not None:
            client.close()
        if tmp_key:
            with suppress(OSError):
                os.unlink(tmp_key)

    command = (
        "export GIT_SSH_COMMAND="
        + shlex.quote(f"ssh -i {remote_path} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null")
    )
    return remote_path, command


def _remote_git_setup(server: OdooServer, repo: OdooInstanceGitRepo) -> tuple[str, str]:
    if repo.auth_type == OdooInstanceGitRepo.AuthType.SSH_KEY:
        return _prepare_remote_git_key(server, repo)
    return "", ""


def _with_remote_key_cleanup(command: str, remote_key_path: str) -> str:
    if not remote_key_path:
        return command
    return f"{command} ; rm -f {shlex.quote(remote_key_path)}"


def _remote_repo_head_commit(
    server: OdooServer,
    repo: OdooInstanceGitRepo,
    branch: str,
    *,
    on_line=None,
) -> tuple[str, str]:
    clone_url = _repo_clone_url(repo)
    remote_key_path, git_setup = _remote_git_setup(server, repo)
    branch_ref = f"refs/heads/{(branch or '').strip()}"
    command = (
        f"{git_setup + ' && ' if git_setup else ''}"
        f"git ls-remote {shlex.quote(clone_url)} {shlex.quote(branch_ref)}"
    )
    command = _with_remote_key_cleanup(command, remote_key_path)
    code, output = _ssh_run(server, command, on_line=on_line)
    if code != 0:
        raise RuntimeError(output or f"Could not read the remote commit for branch '{branch}'.")
    match = re.search(r"([0-9a-fA-F]{7,64})", output or "")
    if not match:
        raise RuntimeError(f"Could not determine the remote commit for branch '{branch}'.")
    return match.group(1), output


def _local_repo_head_commit(server: OdooServer, repo_path: str) -> str:
    command = (
        f"if [ -d {shlex.quote(repo_path)}/.git ]; then "
        f"git -C {shlex.quote(repo_path)} rev-parse HEAD; "
        f"fi"
    )
    code, output = _ssh_run(server, command)
    if code != 0:
        return ""
    return (output or "").strip().splitlines()[-1].strip() if (output or "").strip() else ""


def _clean_clone_instance_repo(
    server: OdooServer,
    repo: OdooInstanceGitRepo,
    branch: str,
    *,
    on_line=None,
) -> tuple[str, str]:
    clone_url = _repo_clone_url(repo)
    addons_root = repo.instance.addons_root_path or _instance_runtime_context(repo.instance)["addons_root_path"]
    remote_key_path, git_setup = _remote_git_setup(server, repo)
    clone_cmd = (
        f"mkdir -p {shlex.quote(addons_root)} "
        f"&& rm -rf {shlex.quote(repo.local_path)} "
        f"&& {git_setup + ' && ' if git_setup else ''}"
        f"git clone --branch {shlex.quote(branch)} --single-branch "
        f"{shlex.quote(clone_url)} {shlex.quote(repo.local_path)}"
    )
    clone_cmd = _with_remote_key_cleanup(clone_cmd, remote_key_path)
    code, log_blob = _ssh_run(server, clone_cmd, on_line=on_line)
    if code != 0:
        raise RuntimeError(log_blob or "Clone failed.")

    head_code, head_output = _ssh_run(server, f"git -C {shlex.quote(repo.local_path)} rev-parse HEAD")
    if head_code != 0:
        raise RuntimeError(head_output or "Could not read the cloned commit SHA.")
    return head_output.strip(), log_blob


def _sync_instance_addons_config(instance: OdooInstance, *, on_line=None) -> str:
    addons_root, addons_path = _compute_addons_path(instance)
    runtime = _instance_runtime_context(instance)
    mkdir_cmd = f"mkdir -p {shlex.quote(addons_root)}"
    code, output = _ssh_run(instance.server, mkdir_cmd, on_line=on_line)
    if code != 0:
        raise RuntimeError(output or "Failed to prepare the addons root directory.")
    rewrite_output = _write_remote_config_addons_path(
        instance.server,
        runtime["config_file"],
        addons_path,
        on_line=on_line,
    )
    instance.addons_root_path = addons_root
    instance.addons_path_cache = addons_path
    instance.addons_sync_status = OdooInstance.AddonsSyncStatus.READY
    instance.addons_last_sync_at = timezone.now()
    instance.save(
        update_fields=[
            "addons_root_path",
            "addons_path_cache",
            "addons_sync_status",
            "addons_last_sync_at",
            "updated_at",
        ]
    )
    _broadcast_repo_event(
        instance.id,
        {
            "type": "instance.addons_synced",
            "addons_root_path": instance.addons_root_path,
            "addons_path_cache": instance.addons_path_cache,
            "addons_last_sync_at": instance.addons_last_sync_at.isoformat() if instance.addons_last_sync_at else "",
        },
    )
    return _append_text(output, rewrite_output)


def _initialize_instance_addons_metadata(instance: OdooInstance):
    addons_root, addons_path = _compute_addons_path(instance)
    instance.addons_root_path = addons_root
    instance.addons_path_cache = addons_path
    instance.addons_sync_status = OdooInstance.AddonsSyncStatus.READY
    instance.addons_last_sync_at = timezone.now()


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


def _record_instance_step(instance: OdooInstance, step: str, detail: str = ""):
    message = f"[step] {step}"
    if detail:
        message = f"{message}\n{detail}"
    _record_instance_progress(instance, message)


def _record_instance_error(instance: OdooInstance, heading: str, detail: str):
    _record_instance_progress(instance, f"[error] {heading}\n{detail}".strip())


def _extract_admin_password(log_blob: str) -> str:
    """Pull the generated master admin password from ansible / shell summary output."""
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


def _extract_odoo_admin_user_password(log_blob: str) -> str:
    """
    Pull the Odoo admin *user* login password from ansible output.
    Matches: 'Odoo admin user password: <password>'
    """
    if not log_blob:
        return ""
    match = re.search(r"Odoo admin user password:\s*(.+)", log_blob, flags=re.IGNORECASE)
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
    import requests as _requests
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
    try:
        created = provider.create_server(name=server.name, region=server.region, size=server.size, ssh_key_ids=ssh_key_ids)
    except _requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "?"
        if status_code == 401:
            return False, "", "", "Cloud API returned 401 Unauthorized — check that the API token has write permissions."
        if status_code == 422:
            try:
                detail = exc.response.json().get("message", str(exc))
            except Exception:
                detail = str(exc)
            return False, "", "", f"Cloud API rejected the request ({status_code}): {detail}"
        return False, "", "", f"Cloud API error ({status_code}): {exc}"
    except Exception as exc:
        return False, "", "", f"Failed to create server: {exc}"
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

    args.extend(["-e", json.dumps(merged_vars)])

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


def _default_docker_host_playbook() -> str:
    """Absolute path to the repo-local Docker host bootstrap playbook."""
    return str(Path(settings.BASE_DIR) / "infra" / "ansible" / "setup_docker_host.yml")


def _default_docker_instance_delete_playbook() -> str:
    """Absolute path to the repo-local Docker instance deletion playbook."""
    return str(Path(settings.BASE_DIR) / "infra" / "ansible" / "delete_docker_odoo_instance.yml")


def _default_odoo_instance_delete_playbook() -> str:
    """Absolute path to the repo-local bare-metal instance deletion playbook."""
    return str(Path(settings.BASE_DIR) / "infra" / "ansible" / "delete_odoo_instance_direct.yml")


def _default_enterprise_sync_playbook() -> str:
    """Instance-level: server shared dir → instance path (local copy on server)."""
    return str(Path(settings.BASE_DIR) / "infra" / "ansible" / "sync_odoo_enterprise_addons.yml")


def _default_docker_enterprise_update_playbook() -> str:
    """Docker: re-render compose + force-recreate container with enterprise bind-mount."""
    return str(Path(settings.BASE_DIR) / "infra" / "ansible" / "update_docker_instance_enterprise.yml")


def _default_enterprise_server_sync_playbook() -> str:
    """Server-level: DafeApp host → server shared dir (network upload, done once per server)."""
    return str(Path(settings.BASE_DIR) / "infra" / "ansible" / "sync_enterprise_to_server.yml")


def _server_enterprise_shared_path(server: OdooServer) -> str:
    """
    Canonical path for the Enterprise shared directory on a server.
    All instances on this server copy from here instead of from the DafeApp host.
    """
    stored = server.enterprise_shared_path
    # Reject any stored path that is actually on the DafeApp host filesystem —
    # this can happen if a previous run accidentally stored source.addons_source_path.
    dafeapp_root = str(settings.BASE_DIR)
    if stored and not stored.startswith(dafeapp_root):
        return stored
    summary = server.installation_summary or {}
    odoo_home = summary.get("odoo_home") or f"/opt/odoo{server.odoo_version}"
    return f"{odoo_home}/enterprise_shared"


def _docker_instance_enterprise_host_path(
    instance: OdooInstance,
    server: OdooServer,
    source: "EnterpriseSource",
) -> str:
    """
    Host-side path on the Docker server where enterprise addons live for this instance.

    PLATFORM sources: reuse the server shared dir (one copy, all instances mount it).
    USER sources: per-instance directory so different users don't overwrite each other.
    """
    if source.source_scope == EnterpriseSource.Scope.PLATFORM:
        return _server_enterprise_shared_path(server)
    client_name = instance.db_name.replace("_", "-")
    return f"/opt/odoo-docker/instances/{client_name}-enterprise"


def _sync_enterprise_to_server(
    server: OdooServer,
    source: "EnterpriseSource",
    *,
    on_line=None,
) -> tuple[bool, str]:
    """
    Ensure the server's shared Enterprise directory is up-to-date with `source`.

    - If server already has `source.release_code` synced, returns (True, "") immediately.
    - Otherwise runs sync_enterprise_to_server.yml (DafeApp host → server shared dir).
    - Updates server.enterprise_shared_path and server.enterprise_shared_release_code on success.

    Returns (ok, log_blob).
    """
    release_code = (source.release_code or "").strip()

    # Already up-to-date: skip the network transfer.
    # Also guard against a stored path that is on the DafeApp host (not the server).
    dafeapp_root = str(settings.BASE_DIR)
    shared_path_ok = bool(
        server.enterprise_shared_path
        and not server.enterprise_shared_path.startswith(dafeapp_root)
    )
    if (
        release_code
        and server.enterprise_shared_release_code == release_code
        and shared_path_ok
    ):
        msg = f"Server already has Enterprise release {release_code} at {server.enterprise_shared_path} — skipping upload."
        if on_line:
            on_line(msg)
        return True, msg

    if not source.addons_source_path or not Path(source.addons_source_path).exists():
        return False, "Enterprise source addons path is missing from the DafeApp filesystem."

    playbook = _default_enterprise_server_sync_playbook()
    if not Path(playbook).exists():
        return False, f"Server enterprise sync playbook not found: {playbook}"

    shared_path = _server_enterprise_shared_path(server)
    ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
    try:
        ok, log_blob = _run_ansible_playbook(
            playbook,
            str(server.ip_address),
            {
                "enterprise_src": source.addons_source_path,
                "enterprise_dest": shared_path,
                # Docker hosts have no 'odoo' OS user — pass empty so the playbook uses root.
                "odoo_user": "" if server.deployment_mode == OdooServer.DeploymentMode.DOCKER else "odoo",
            },
            ssh_user=ssh_user,
            ssh_key_path=ssh_key,
            ssh_password=ssh_password,
            on_chunk=on_line,
        )
    finally:
        if tmp_key:
            with suppress(OSError):
                os.unlink(tmp_key)

    if ok:
        server.enterprise_shared_path = shared_path
        server.enterprise_shared_release_code = release_code
        server.save(update_fields=["enterprise_shared_path", "enterprise_shared_release_code", "updated_at"])

    return ok, log_blob


def _default_bare_traefik_gateway_playbook() -> str:
    return str(Path(settings.BASE_DIR) / "infra" / "ansible" / "setup_bare_traefik_gateway.yml")


def _default_bare_traefik_route_playbook() -> str:
    return str(Path(settings.BASE_DIR) / "infra" / "ansible" / "apply_bare_traefik_route.yml")


def _default_bare_traefik_route_delete_playbook() -> str:
    return str(Path(settings.BASE_DIR) / "infra" / "ansible" / "delete_bare_traefik_route.yml")


def _traefik_dynamic_dir() -> str:
    return getattr(settings, "TRAEFIK_DYNAMIC_CONFIG_DIR", "/etc/traefik/dynamic")


def _traefik_acme_email() -> str:
    return getattr(settings, "TRAEFIK_ACME_EMAIL", os.getenv("ODOO_ADMIN_EMAIL", "odoo@example.com").strip())


def _cleanup_acme_challenge_dns_records(base_domain: str, cf_token: str) -> None:
    """
    Delete all _acme-challenge TXT records for *.base_domain from Cloudflare.
    Stale records accumulate when Traefik's ACME cleanup fails (e.g. after a
    forced acme.json reset), causing error 81058 on the next cert request.
    """
    if not cf_token or not base_domain:
        return
    import requests as _requests

    headers = {"Authorization": f"Bearer {cf_token}", "Content-Type": "application/json"}
    base = "https://api.cloudflare.com/client/v4"

    # Resolve zone ID by domain name (avoid requiring PLATFORM_DNS_ZONE_ID)
    try:
        resp = _requests.get(f"{base}/zones", params={"name": base_domain}, headers=headers, timeout=10)
        zones = resp.json().get("result", [])
        if not zones:
            logger.warning("_cleanup_acme_challenge_dns_records: zone not found for %s", base_domain)
            return
        zone_id = zones[0]["id"]
    except Exception as exc:
        logger.warning("_cleanup_acme_challenge_dns_records: failed to resolve zone: %s", exc)
        return

    # List all TXT records that start with _acme-challenge
    try:
        resp = _requests.get(
            f"{base}/zones/{zone_id}/dns_records",
            params={"type": "TXT", "per_page": 100},
            headers=headers,
            timeout=10,
        )
        records = resp.json().get("result", [])
    except Exception as exc:
        logger.warning("_cleanup_acme_challenge_dns_records: failed to list records: %s", exc)
        return

    deleted = 0
    for record in records:
        if "_acme-challenge" in record.get("name", ""):
            try:
                _requests.delete(f"{base}/zones/{zone_id}/dns_records/{record['id']}", headers=headers, timeout=10)
                deleted += 1
                logger.info("_cleanup_acme_challenge_dns_records: deleted %s", record["name"])
            except Exception as exc:
                logger.warning("_cleanup_acme_challenge_dns_records: failed to delete %s: %s", record["name"], exc)
    if deleted:
        logger.info("_cleanup_acme_challenge_dns_records: cleaned %d stale record(s) for %s", deleted, base_domain)


def _effective_tls_mode(server: OdooServer) -> str:
    value = getattr(server, "tls_mode", "") or getattr(settings, "TRAEFIK_DEFAULT_TLS_MODE", OdooServer.TLSMode.LETS_ENCRYPT)
    valid = {choice for choice, _ in OdooServer.TLSMode.choices}
    return value if value in valid else OdooServer.TLSMode.LETS_ENCRYPT


def _route_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_domain_name(value)).strip("-")
    return slug or f"route-{int(time.time())}"


def _route_file_path(domain: str) -> str:
    return f"{_traefik_dynamic_dir().rstrip('/')}/dafeapp-{_route_slug(domain)}.yml"


def _active_domain_assignment(instance: OdooInstance):
    return instance.domain_assignments.exclude(status=DomainAssignment.Status.DELETED).order_by("-is_primary", "-created_at", "-id").first()


def _active_domain_assignments(instance: OdooInstance):
    return list(instance.domain_assignments.exclude(status=DomainAssignment.Status.DELETED).order_by("-is_primary", "-created_at", "-id"))


def _ensure_domain_assignment(instance: OdooInstance, domain: str | None = None):
    domain = normalize_domain_name(domain or instance.domain)
    if not domain:
        return None

    source = (
        DomainAssignment.Source.PLATFORM
        if platform_base_domain() and domain.endswith(f".{platform_base_domain()}")
        else DomainAssignment.Source.CUSTOM
    )
    preferred_zone = instance.server.managed_dns_zone if instance.server_id and source == DomainAssignment.Source.CUSTOM else None
    zone = DnsZone.match_for_domain(instance.organization, domain, preferred_zone=preferred_zone)
    assignment = instance.domain_assignments.exclude(status=DomainAssignment.Status.DELETED).filter(domain=domain).first()
    if assignment and assignment.domain != domain:
        assignment.status = DomainAssignment.Status.DELETED
        assignment.instance = None
        assignment.last_error = ""
        assignment.last_synced_at = timezone.now()
        assignment.save(update_fields=["status", "instance", "last_error", "last_synced_at", "updated_at"])
        assignment = None

    if assignment is None:
        assignment = DomainAssignment(
            organization=instance.organization,
            instance=instance,
        )

    assignment.zone = zone
    assignment.domain = domain
    assignment.source = source
    assignment.is_primary = domain == normalize_domain_name(instance.domain)
    assignment.hostname = zone.hostname_for_domain(domain) if zone else domain
    assignment.proxied = bool(zone.default_proxied) if zone else False
    assignment.is_managed = bool(
        zone
        and instance.server.managed_dns_enabled
        and (instance.server.managed_dns_zone_id is None or instance.server.managed_dns_zone_id == zone.id)
    )
    if source == DomainAssignment.Source.PLATFORM:
        assignment.zone = None
        assignment.hostname = domain
        assignment.proxied = platform_dns_default_proxied()
        assignment.is_managed = platform_dns_is_configured()
    assignment.status = DomainAssignment.Status.PENDING
    assignment.last_error = ""
    assignment.instance = instance
    assignment.save()
    return assignment


def _save_instance_domain_state(
    instance: OdooInstance,
    *,
    domain_status: str | None = None,
    ssl_status: str | None = None,
    ssl_enabled: bool | None = None,
    ssl_error: str | None = None,
    checked_at=None,
):
    update_fields = ["updated_at"]
    if domain_status is not None:
        instance.domain_status = domain_status
        update_fields.append("domain_status")
    if ssl_status is not None:
        instance.ssl_status = ssl_status
        update_fields.append("ssl_status")
    if ssl_enabled is not None:
        instance.ssl_enabled = ssl_enabled
        update_fields.append("ssl_enabled")
    if ssl_error is not None:
        instance.ssl_error = ssl_error
        update_fields.append("ssl_error")
    if checked_at is not None:
        instance.domain_last_checked_at = checked_at
        update_fields.append("domain_last_checked_at")
    instance.save(update_fields=list(dict.fromkeys(update_fields)))


_DNS_RECORD_UNSET = object()


def _save_assignment_state(
    assignment: DomainAssignment | None,
    *,
    status: str | None = None,
    last_error: str | None = None,
    last_synced_at=None,
    dns_record=_DNS_RECORD_UNSET,
):
    if assignment is None:
        return
    update_fields = ["updated_at"]
    if status is not None:
        assignment.status = status
        update_fields.append("status")
    if last_error is not None:
        assignment.last_error = last_error
        update_fields.append("last_error")
    if last_synced_at is not None:
        assignment.last_synced_at = last_synced_at
        update_fields.append("last_synced_at")
    if dns_record is not _DNS_RECORD_UNSET:
        assignment.dns_record = dns_record
        update_fields.append("dns_record")
    assignment.save(update_fields=list(dict.fromkeys(update_fields)))


def _mark_instance_domain_failed(instance: OdooInstance, assignment: DomainAssignment | None, message: str):
    now = timezone.now()
    ssl_status = (
        OdooInstance.SSLStatus.NOT_CONFIGURED
        if _effective_tls_mode(instance.server) == OdooServer.TLSMode.DISABLED
        else OdooInstance.SSLStatus.FAILED
    )
    _save_instance_domain_state(
        instance,
        domain_status=OdooInstance.DomainStatus.FAILED,
        ssl_status=ssl_status,
        ssl_enabled=False,
        ssl_error=message,
        checked_at=now,
    )
    _save_assignment_state(
        assignment,
        status=DomainAssignment.Status.FAILED,
        last_error=message,
        last_synced_at=now,
    )


def _probe_domain_access(instance: OdooInstance, domain: str | None = None) -> tuple[bool, bool, str]:
    import ssl as ssl_lib
    import urllib.error
    import urllib.request

    domain = normalize_domain_name(domain or instance.domain)
    if not domain:
        return False, False, "No domain configured."

    use_ssl = _effective_tls_mode(instance.server) != OdooServer.TLSMode.DISABLED
    scheme = "https" if use_ssl else "http"
    url = f"{scheme}://{domain}/web/health"

    # First pass: unverified context — confirms the server is reachable and Odoo responds.
    context = ssl_lib._create_unverified_context() if use_ssl else None
    try:
        with urllib.request.urlopen(url, timeout=8, context=context) as response:
            ok = response.status == 200
    except Exception as exc:
        return False, False, str(exc)

    if not ok:
        return False, False, f"Domain health check returned HTTP {response.status} for {url}."

    if not use_ssl:
        return True, False, f"Domain health check succeeded for {url}."

    # Second pass: verified context — confirms the cert is signed by a trusted CA,
    # not Traefik's internal self-signed fallback (which browsers reject).
    try:
        verified_ctx = ssl_lib.create_default_context()
        with urllib.request.urlopen(url, timeout=8, context=verified_ctx) as _:
            pass
        return True, True, f"Domain health check succeeded (valid TLS certificate) for {url}."
    except ssl_lib.SSLCertVerificationError:
        return True, False, (
            f"Domain is reachable but the TLS certificate is not yet trusted "
            f"(Let's Encrypt may still be issuing it) for {url}."
        )
    except Exception:
        return True, False, f"Domain is reachable but TLS certificate could not be verified for {url}."


def _ensure_bare_traefik_gateway(server: OdooServer, *, acme_reset: bool = False) -> tuple[bool, str]:
    if server.deployment_mode != OdooServer.DeploymentMode.BARE_METAL:
        return True, "Traefik gateway not required for this deployment mode."
    if not server.ip_address:
        return False, "Server IP is missing; cannot configure the Traefik gateway."
    cache_key = f"deployments:server:{server.id}:traefik-gateway-ready"
    if cache.get(cache_key):
        return True, "Traefik gateway recently reconciled."

    playbook = os.getenv("ANSIBLE_BARE_TRAEFIK_GATEWAY_PLAYBOOK", "").strip() or _default_bare_traefik_gateway_playbook()
    if not Path(playbook).exists():
        return False, f"Bare-metal Traefik playbook not found: {playbook}"

    extra_vars = {
        "traefik_dynamic_dir": _traefik_dynamic_dir(),
        "traefik_tls_mode": _effective_tls_mode(server),
        "traefik_acme_email": _traefik_acme_email(),
        "traefik_acme_storage": getattr(settings, "TRAEFIK_ACME_STORAGE", "/var/lib/traefik/acme.json"),
        "traefik_log_level": getattr(settings, "TRAEFIK_LOG_LEVEL", "INFO"),
        "traefik_version": getattr(settings, "TRAEFIK_VERSION", "3.1.2"),
        "traefik_acme_reset": "true" if acme_reset else "false",
        "cf_dns_api_token": os.getenv("PLATFORM_DNS_API_TOKEN", "").strip(),
        "traefik_base_domain": os.getenv("PLATFORM_BASE_DOMAIN", "").strip(),
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
    if ok:
        cache.set(cache_key, True, 300)
    return ok, log_blob


def _apply_bare_traefik_route(instance: OdooInstance, server: OdooServer, domain: str | None = None) -> tuple[bool, str]:
    resolved_domain = normalize_domain_name(domain or instance.domain)
    if not server.ip_address:
        return False, "Server IP is missing; cannot apply the Traefik route."
    playbook = os.getenv("ANSIBLE_BARE_TRAEFIK_ROUTE_PLAYBOOK", "").strip() or _default_bare_traefik_route_playbook()
    if not Path(playbook).exists():
        return False, f"Bare-metal Traefik route playbook not found: {playbook}"

    ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
    extra_vars = {
        "domain": resolved_domain,
        "http_port": instance.http_port,
        "route_name": _route_slug(resolved_domain),
        "route_file": _route_file_path(resolved_domain),
        "traefik_dynamic_dir": _traefik_dynamic_dir(),
        "traefik_tls_mode": _effective_tls_mode(server),
    }
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
    return ok, log_blob


def _delete_bare_traefik_route(instance: OdooInstance, server: OdooServer, domain: str | None = None) -> tuple[bool, str]:
    resolved_domain = normalize_domain_name(domain or instance.domain)
    if not server.ip_address:
        return False, "Server IP is missing; skipped Traefik route removal."
    playbook = os.getenv("ANSIBLE_BARE_TRAEFIK_ROUTE_DELETE_PLAYBOOK", "").strip() or _default_bare_traefik_route_delete_playbook()
    if not Path(playbook).exists():
        return False, f"Bare-metal Traefik route delete playbook not found: {playbook}"

    ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
    extra_vars = {
        "domain": resolved_domain,
        "route_name": _route_slug(resolved_domain),
        "route_file": _route_file_path(resolved_domain),
        "traefik_dynamic_dir": _traefik_dynamic_dir(),
    }
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
    return ok, log_blob


def _upsert_managed_dns_record(instance: OdooInstance, assignment: DomainAssignment | None):
    if assignment is None:
        return None, "No domain assignment is available."

    if assignment.source == DomainAssignment.Source.PLATFORM:
        if not platform_dns_is_configured():
            return None, "Platform DNS is not configured."
        if not instance.server.ip_address:
            raise RuntimeError("Server IP is not available for DNS record provisioning.")

        provider = platform_dns_provider_service()
        zone_name = platform_base_domain()
        payload = provider.upsert_record(
            getattr(settings, "PLATFORM_DNS_ZONE_ID", "").strip(),
            record_type=DnsRecord.RecordType.A,
            name=assignment.domain,
            content=str(instance.server.ip_address),
            proxied=assignment.proxied,
            ttl=1,
        )
        now = timezone.now()
        assignment.dns_record = None
        assignment.is_managed = True
        assignment.provider_record_id = str(payload.get("id") or "")
        assignment.last_error = ""
        assignment.last_synced_at = now
        assignment.save(update_fields=["dns_record", "is_managed", "provider_record_id", "last_error", "last_synced_at", "updated_at"])
        return None, f"Platform DNS record ensured for {assignment.domain} (provider id: {payload.get('id') or 'n/a'})."

    if not instance.server.managed_dns_enabled:
        return None, "Managed DNS disabled for this server."

    zone = assignment.zone or instance.server.managed_dns_zone
    if zone is None:
        raise RuntimeError("Managed DNS is enabled, but no matching DNS zone was found for this domain.")
    if instance.server.managed_dns_zone_id and zone.id != instance.server.managed_dns_zone_id:
        raise RuntimeError(f"{assignment.domain} does not belong to the server's configured DNS zone.")
    if not zone.provider_zone_id:
        raise RuntimeError(f"DNS zone {zone.name} is missing its provider zone id. Sync the zone first.")
    if not instance.server.ip_address:
        raise RuntimeError("Server IP is not available for DNS record provisioning.")

    provider = get_dns_provider_service(zone.provider_account)
    payload = provider.upsert_record(
        zone.provider_zone_id,
        record_type=DnsRecord.RecordType.A,
        name=assignment.domain,
        content=str(instance.server.ip_address),
        proxied=assignment.proxied,
        ttl=1,
    )
    now = timezone.now()
    record, _ = DnsRecord.objects.update_or_create(
        organization=instance.organization,
        zone=zone,
        record_type=DnsRecord.RecordType.A,
        hostname=zone.hostname_for_domain(assignment.domain),
        defaults={
            "value": str(instance.server.ip_address),
            "ttl": 1,
            "proxied": assignment.proxied,
            "provider_record_id": str(payload.get("id") or ""),
            "status": DnsRecord.Status.ACTIVE,
            "last_error": "",
            "last_synced_at": now,
        },
    )
    assignment.zone = zone
    assignment.is_managed = True
    assignment.dns_record = record
    assignment.provider_record_id = str(payload.get("id") or "")
    assignment.last_error = ""
    assignment.last_synced_at = now
    assignment.save(update_fields=["zone", "is_managed", "dns_record", "provider_record_id", "last_error", "last_synced_at", "updated_at"])
    return record, f"Managed DNS record ensured for {assignment.domain}."


def _delete_managed_dns_record(assignment: DomainAssignment | None) -> tuple[bool, str]:
    if assignment is None:
        return True, "No managed DNS record to remove."

    # PLATFORM records store the Cloudflare record id directly on the assignment
    # (dns_record FK is intentionally null for platform records — must check this
    # BEFORE the dns_record_id guard below).
    if assignment.source == DomainAssignment.Source.PLATFORM:
        if not assignment.provider_record_id:
            return True, "No platform DNS record to remove."
        try:
            provider = platform_dns_provider_service()
            provider.delete_record(getattr(settings, "PLATFORM_DNS_ZONE_ID", "").strip(), assignment.provider_record_id)
        except Exception as exc:
            return False, str(exc)
        assignment.provider_record_id = ""
        assignment.dns_record = None
        assignment.is_managed = False
        assignment.last_synced_at = timezone.now()
        assignment.save(update_fields=["provider_record_id", "dns_record", "is_managed", "last_synced_at", "updated_at"])
        return True, f"Platform DNS record removed for {assignment.domain}."

    # CUSTOM / managed-zone records track the Cloudflare record via DnsRecord FK
    if not assignment.dns_record_id:
        return True, "No managed DNS record to remove."

    record = assignment.dns_record
    if not record:
        return True, "No managed DNS record to remove."

    try:
        if record.provider_record_id and assignment.zone_id and assignment.zone.provider_zone_id:
            provider = get_dns_provider_service(assignment.zone.provider_account)
            provider.delete_record(assignment.zone.provider_zone_id, record.provider_record_id)
    except Exception as exc:
        now = timezone.now()
        record.status = DnsRecord.Status.FAILED
        record.last_error = str(exc)
        record.last_synced_at = now
        record.save(update_fields=["status", "last_error", "last_synced_at", "updated_at"])
        return False, str(exc)

    now = timezone.now()
    record.status = DnsRecord.Status.DELETED
    record.last_error = ""
    record.last_synced_at = now
    record.save(update_fields=["status", "last_error", "last_synced_at", "updated_at"])
    assignment.dns_record = None
    assignment.is_managed = False
    assignment.provider_record_id = ""
    assignment.last_synced_at = now
    assignment.save(update_fields=["dns_record", "is_managed", "provider_record_id", "last_synced_at", "updated_at"])
    return True, f"Managed DNS record removed for {assignment.domain}."


def _reconcile_assignment_domain(
    instance: OdooInstance,
    assignment: DomainAssignment,
    *,
    skip_probe: bool = False,
) -> tuple[bool, str]:
    now = timezone.now()
    domain = normalize_domain_name(assignment.domain)
    if not domain:
        if assignment.is_primary:
            _save_instance_domain_state(
                instance,
                domain_status=OdooInstance.DomainStatus.NOT_CONFIGURED,
                ssl_status=OdooInstance.SSLStatus.NOT_CONFIGURED,
                ssl_enabled=False,
                ssl_error="",
                checked_at=now,
            )
        return True, "No domain configured."

    messages: list[str] = []

    # For already-ACTIVE assignments skip the Ansible re-application steps.
    # The periodic reconcile task includes ACTIVE domains (to probe health) but
    # re-running Traefik playbooks can transiently fail and flip SSL → FAILED,
    # undoing a working setup.  When an assignment is already ACTIVE we only
    # need to verify it is still reachable via the probe below.
    already_active = assignment.status == DomainAssignment.Status.ACTIVE

    if instance.server.deployment_mode == OdooServer.DeploymentMode.BARE_METAL and not already_active:
        ok, route_log = _ensure_bare_traefik_gateway(instance.server)
        if not ok:
            if assignment.is_primary:
                _mark_instance_domain_failed(instance, assignment, route_log)
            else:
                _save_assignment_state(assignment, status=DomainAssignment.Status.FAILED, last_error=route_log, last_synced_at=now)
            return False, route_log
        if route_log:
            messages.append(route_log)

        ok, route_log = _apply_bare_traefik_route(instance, instance.server, domain)
        if not ok:
            if assignment.is_primary:
                _mark_instance_domain_failed(instance, assignment, route_log)
            else:
                _save_assignment_state(assignment, status=DomainAssignment.Status.FAILED, last_error=route_log, last_synced_at=now)
            return False, route_log
        if route_log:
            messages.append(route_log)

    try:
        _, dns_message = _upsert_managed_dns_record(instance, assignment)
        if dns_message and "disabled" not in dns_message.lower():
            messages.append(dns_message)
    except Exception as exc:
        if already_active:
            # Don't demote an already-working domain on a transient DNS API failure.
            logger.warning("DNS upsert failed for active assignment %s (domain %s): %s", assignment.id, domain, exc)
            messages.append(f"DNS update skipped (will retry): {exc}")
        else:
            if assignment.is_primary:
                _mark_instance_domain_failed(instance, assignment, str(exc))
            else:
                _save_assignment_state(assignment, status=DomainAssignment.Status.FAILED, last_error=str(exc), last_synced_at=now)
            return False, str(exc)

    if skip_probe:
        if assignment.is_primary:
            _save_instance_domain_state(
                instance,
                domain_status=OdooInstance.DomainStatus.PENDING,
                ssl_status=(
                    OdooInstance.SSLStatus.NOT_CONFIGURED
                    if _effective_tls_mode(instance.server) == OdooServer.TLSMode.DISABLED
                    else OdooInstance.SSLStatus.PENDING
                ),
                ssl_enabled=False,
                ssl_error="",
                checked_at=now,
            )
        _save_assignment_state(
            assignment,
            status=DomainAssignment.Status.PENDING,
            last_error="",
            last_synced_at=now,
        )
        return True, " ".join(filter(None, messages)) or "Domain provisioning queued."

    ok, ssl_active, probe_message = _probe_domain_access(instance, domain)
    tls_configured = _effective_tls_mode(instance.server) != OdooServer.TLSMode.DISABLED
    if ok:
        # Cert not yet valid on a TLS-enabled bare-metal server: keep assignment PENDING
        # so the gateway + route are re-applied on the next reconciliation cycle until
        # Let's Encrypt issues the certificate.  Also clear the gateway cache so the
        # playbook runs unconditionally (it will restart Traefik if config changed, which
        # wipes ACME backoff state and triggers a fresh certificate request).
        cert_pending = tls_configured and not ssl_active and instance.server.deployment_mode == OdooServer.DeploymentMode.BARE_METAL
        if cert_pending:
            cache.delete(f"deployments:server:{instance.server.id}:traefik-gateway-ready")

        if assignment.is_primary:
            if ssl_active:
                computed_ssl_status = OdooInstance.SSLStatus.ACTIVE
            elif tls_configured:
                # Server reachable but cert not yet trusted (self-signed fallback from Traefik
                # while Let's Encrypt issues the certificate).
                computed_ssl_status = OdooInstance.SSLStatus.PENDING
            else:
                computed_ssl_status = OdooInstance.SSLStatus.NOT_CONFIGURED
            _save_instance_domain_state(
                instance,
                domain_status=OdooInstance.DomainStatus.ACTIVE,
                ssl_status=computed_ssl_status,
                ssl_enabled=ssl_active,
                ssl_error="" if ssl_active else probe_message,
                checked_at=now,
            )
        _save_assignment_state(
            assignment,
            status=DomainAssignment.Status.PENDING if cert_pending else DomainAssignment.Status.ACTIVE,
            last_error="" if ssl_active else probe_message,
            last_synced_at=now,
        )
        messages.append(probe_message)
        return True, " ".join(filter(None, messages))

    if assignment.is_primary:
        _save_instance_domain_state(
            instance,
            domain_status=OdooInstance.DomainStatus.PENDING,
            ssl_status=(
                OdooInstance.SSLStatus.NOT_CONFIGURED
                if _effective_tls_mode(instance.server) == OdooServer.TLSMode.DISABLED
                else OdooInstance.SSLStatus.PENDING
            ),
            ssl_enabled=False,
            ssl_error=probe_message if ssl_active else "",
            checked_at=now,
        )
    _save_assignment_state(
        assignment,
        status=DomainAssignment.Status.PENDING,
        last_error=probe_message,
        last_synced_at=now,
    )
    messages.append(probe_message)
    return True, " ".join(filter(None, messages))


def _reconcile_instance_domain(instance: OdooInstance, *, skip_probe: bool = False) -> tuple[bool, str]:
    assignments = _active_domain_assignments(instance)
    if not assignments and instance.domain:
        assignment = _ensure_domain_assignment(instance)
        assignments = [assignment] if assignment else []

    if not assignments:
        _save_instance_domain_state(
            instance,
            domain_status=OdooInstance.DomainStatus.NOT_CONFIGURED,
            ssl_status=OdooInstance.SSLStatus.NOT_CONFIGURED,
            ssl_enabled=False,
            ssl_error="",
            checked_at=timezone.now(),
        )
        return True, "No domain configured."

    results = []
    overall_ok = True
    for assignment in assignments:
        ok, message = _reconcile_assignment_domain(instance, assignment, skip_probe=skip_probe)
        overall_ok = overall_ok and ok
        if message:
            results.append(message)
    return overall_ok, " ".join(results)


def _detach_instance_domain_overlay(instance: OdooInstance, domain: str | None = None) -> tuple[bool, str]:
    now = timezone.now()
    target_domain = normalize_domain_name(domain or "")
    assignment = (
        instance.domain_assignments.exclude(status=DomainAssignment.Status.DELETED).filter(domain=target_domain).first()
        if target_domain
        else _active_domain_assignment(instance)
    )
    messages: list[str] = []
    ok = True

    if assignment and assignment.domain and instance.server.deployment_mode == OdooServer.DeploymentMode.BARE_METAL:
        route_ok, route_message = _delete_bare_traefik_route(instance, instance.server, assignment.domain)
        ok = ok and route_ok
        if route_message:
            messages.append(route_message)

    record_ok, record_message = _delete_managed_dns_record(assignment)
    ok = ok and record_ok
    if record_message:
        messages.append(record_message)

    if assignment is not None:
        assignment.status = DomainAssignment.Status.DELETED
        assignment.instance = None
        assignment.last_error = ""
        assignment.last_synced_at = now
        assignment.save(update_fields=["status", "instance", "last_error", "last_synced_at", "updated_at"])

    if assignment is not None and assignment.is_primary:
        instance.domain = ""
        instance.domain_status = OdooInstance.DomainStatus.NOT_CONFIGURED
        instance.domain_last_checked_at = now
        instance.ssl_status = OdooInstance.SSLStatus.NOT_CONFIGURED
        instance.ssl_enabled = False
        instance.ssl_error = ""
        instance.save(
            update_fields=[
                "domain",
                "domain_status",
                "domain_last_checked_at",
                "ssl_status",
                "ssl_enabled",
                "ssl_error",
                "updated_at",
            ]
        )
    return ok, " ".join(filter(None, messages))


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


def _mark_server_timed_out(server_id: int, task_name: str):
    """Mark a server FAILED when its Celery task exceeds the soft time limit."""
    try:
        server = OdooServer.objects.get(pk=server_id)
        if server.status not in (OdooServer.Status.PROVISIONED, OdooServer.Status.FAILED, OdooServer.Status.ARCHIVED):
            server.status = OdooServer.Status.FAILED
            server.celery_task_id = ""
            server.provisioning_log = _append_text(
                server.provisioning_log,
                f"[timeout] Task {task_name} exceeded the 25-minute time limit and was stopped automatically.",
            )
            server.save(update_fields=["status", "celery_task_id", "provisioning_log", "updated_at"])
            _broadcast_server(server.id, "Provisioning timed out — server marked as Failed.", server.status, done=True)
    except Exception:
        logger.exception("Could not mark server %s as timed-out", server_id)


@shared_task(bind=True, max_retries=0, time_limit=1800, soft_time_limit=1500)
def provision_odoo_server(self, server_id: int):
    try:
     return _provision_odoo_server_inner(self, server_id)
    except SoftTimeLimitExceeded:
        _mark_server_timed_out(server_id, "provision_odoo_server")


def _provision_odoo_server_inner(self, server_id: int):
    server = OdooServer.objects.select_related(
        "organization",
        "cloud_account",
        "infrastructure",
        "infrastructure__cloud_account",
        "infrastructure__external_server",
    ).get(pk=server_id)
    org = server.organization

    server.status = OdooServer.Status.CONNECTING
    server.celery_task_id = self.request.id or ""
    server.installation_summary = {}
    server.installation_summary_text = ""
    server.provisioning_log = ""
    server.save(update_fields=["status", "celery_task_id", "installation_summary", "installation_summary_text", "provisioning_log", "updated_at"])
    infra = server.infrastructure
    logger.info(
        "Server provisioning started: id=%s name=%s version=%s mode=%s infra=%s",
        server.id,
        server.name,
        server.odoo_version,
        server.deployment_mode,
        getattr(infra, "infra_type", "unknown"),
    )
    _broadcast_server(server.id, "Checking connectivity…", server.status)
    if not infra:
        logger.error("Server %s provisioning aborted: missing infrastructure record.", server.id)
        server.status = OdooServer.Status.FAILED
        server.provisioning_log = _append_text(server.provisioning_log, "Server is missing infrastructure.")
        server.celery_task_id = ""
        server.save(update_fields=["status", "celery_task_id", "provisioning_log", "updated_at"])
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
        ext.verification_error = "Checking connection..."
        ext.save(update_fields=["last_verified_at", "verification_error"])
        _broadcast_server(server.id, "Checking connection…", server.status)
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
        server.provisioning_log = _append_text(server.provisioning_log, "Connection verified." if reachable else err)
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
            server.celery_task_id = ""
            server.save(update_fields=["status", "celery_task_id", "provisioning_log", "updated_at"])
            _broadcast_server(server.id, f"Failed to connect: {err}", server.status, done=True)
            return

        logger.info(
            "Server %s: PYOS reachability confirmed for %s:%s",
            server.id,
            ext.host,
            ext.port or 22,
        )
        server.status = OdooServer.Status.PROVISIONING
        server.save(
            update_fields=["status", "updated_at"]
        )
        _broadcast_server(server.id, f"Connection confirmed ({ext.host}) — starting Odoo configuration…", server.status)
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

    # CONNECTING phase: live API credential check before starting Terraform
    logger.info("Server %s: validating managed cloud credentials via live API call", server.id)
    _broadcast_server(server.id, "Connecting to cloud provider…", server.status)

    # Step 1: DB sanity check (account exists and is marked verified)
    ok, err = infra.validate_connection_target()
    if not ok:
        logger.warning("Server %s: infrastructure DB validation failed: %s", server.id, err)
        server.status = OdooServer.Status.FAILED
        server.celery_task_id = ""
        server.provisioning_log = _append_text(server.provisioning_log, err)
        server.save(update_fields=["status", "celery_task_id", "provisioning_log", "updated_at"])
        _broadcast_server(server.id, f"Failed to connect: {err}", server.status, done=True)
        return

    # Step 2: live API call — actually test the credentials with the provider
    cloud_account = infra.cloud_account
    try:
        from cloud.providers import get_provider
        provider = get_provider(cloud_account)
        api_ok, api_err = provider.validate_credentials()
    except Exception as exc:
        api_ok, api_err = False, str(exc)

    if not api_ok:
        logger.warning("Server %s: cloud API credential check failed: %s", server.id, api_err)
        server.status = OdooServer.Status.FAILED
        server.celery_task_id = ""
        server.provisioning_log = _append_text(server.provisioning_log, api_err)
        server.save(update_fields=["status", "celery_task_id", "provisioning_log", "updated_at"])
        _broadcast_server(server.id, f"Failed to connect: {api_err}", server.status, done=True)
        return

    logger.info("Server %s: cloud API credentials confirmed (%s)", server.id, cloud_account.provider)

    # Transition to PROVISIONING for Terraform / droplet creation phase
    server.status = OdooServer.Status.PROVISIONING
    server.save(update_fields=["status", "updated_at"])
    _broadcast_server(server.id, f"Connected to {cloud_account.provider} — starting droplet creation…", server.status)

    state_root = Path(settings.BASE_DIR) / ".terraform_state" / f"org_{org.id}" / f"odoo_server_{server.id}"
    state_root.mkdir(parents=True, exist_ok=True)

    tf_dir = os.getenv("TERRAFORM_SERVER_MODULE_DIR", "").strip()
    _tf_candidate = Path(tf_dir) if tf_dir else None
    module_dir = _tf_candidate if (_tf_candidate and (_tf_candidate / "main.tf").exists()) else state_root

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
            server.celery_task_id = ""
            server.save(update_fields=["status", "celery_task_id", "updated_at"])
            _broadcast_server(server.id, "Terraform init failed — check logs.", server.status, done=True)
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
            server.celery_task_id = ""
            server.save(update_fields=["status", "celery_task_id", "updated_at"])
            _broadcast_server(server.id, "Terraform apply failed — check logs.", server.status, done=True)
            return

        ip = _extract_public_ip(module_dir, extra_env=tf_env)
        if not ip:
            ok, provider_id, ip, err = _provider_native_provision_server(server)
            if not ok:
                server.status = OdooServer.Status.FAILED
                server.celery_task_id = ""
                server.provisioning_log = _append_text(server.provisioning_log, err)
                server.save(update_fields=["status", "celery_task_id", "provisioning_log", "updated_at"])
                _broadcast_server(server.id, "Failed to provision server.", server.status, done=True)
                return
            server.provider_server_id = provider_id
        else:
            server.firewall_configured = True
        server.ip_address = ip or None
    else:
        ok, provider_id, ip, err = _provider_native_provision_server(server)
        if not ok:
            server.status = OdooServer.Status.FAILED
            server.celery_task_id = ""
            server.provisioning_log = _append_text(server.provisioning_log, "Provider fallback provisioning failed.")
            server.provisioning_log = _append_text(server.provisioning_log, err)
            server.save(update_fields=["status", "celery_task_id", "provisioning_log", "updated_at"])
            _broadcast_server(server.id, "Failed to provision server.", server.status, done=True)
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

    # For managed (Terraform-provisioned) servers the SSH daemon is not yet ready
    # immediately after `terraform apply` — the server needs 30-60 s to boot.
    # Ansible (configure_odoo_server / configure_docker_host) handles SSH retries
    # internally, so we go straight to CONFIGURING here.
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
    _broadcast_server(server.id, f"Infrastructure ready ({server.ip_address}) — starting configuration…", server.status)

    if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
        _queue_or_run(configure_docker_host, server.id)
    else:
        _queue_or_run(configure_odoo_server, server.id)


@shared_task(bind=True, max_retries=0, time_limit=1800, soft_time_limit=1500)
def configure_odoo_server(self, server_id: int, job_id: int | None = None):
    try:
        return _configure_odoo_server_inner(self, server_id, job_id)
    except SoftTimeLimitExceeded:
        _mark_server_timed_out(server_id, "configure_odoo_server")


def _configure_odoo_server_inner(self, server_id: int, job_id: int | None = None):
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

    _pb = os.getenv("ANSIBLE_ODOO_SERVER_PLAYBOOK", "").strip()
    playbook = _pb if (_pb and Path(_pb).exists()) else _default_odoo_server_playbook()
    if not Path(playbook).exists():
        logger.error("Server %s: bootstrap playbook not found: %s", server.id, playbook)
        server.status = OdooServer.Status.FAILED
        msg = f"Server bootstrap playbook not found: {playbook}"
        server.provisioning_log = _append_text(server.provisioning_log, msg)
        server.save(update_fields=["status", "installation_summary", "installation_summary_text", "provisioning_log", "updated_at"])
        _job_done(job_id, ok=False, log=msg)
        return
    logger.info("Server %s: running Ansible playbook %s", server.id, playbook)

    # Wait for SSH to be ready before running Ansible.
    # The cloud provider marks the server "active" as soon as the VM boots,
    # but sshd can take another 30–90 s to start.
    ws_group = f"odoo.server.{server.id}"
    ip_str = str(server.ip_address)
    _broadcast_server(server.id, "Waiting for SSH to become available…", server.status)
    ssh_ready = False
    for attempt in range(60):  # up to 5 minutes (60 × 5 s)
        try:
            with socket.create_connection((ip_str, 22), timeout=5):
                ssh_ready = True
                break
        except OSError:
            if attempt % 6 == 0:  # log every 30 s
                logger.info("Server %s: SSH not ready yet (attempt %s/60)…", server.id, attempt + 1)
            time.sleep(5)

    if not ssh_ready:
        logger.error("Server %s: SSH did not become available after 5 minutes.", server.id)
        server.status = OdooServer.Status.FAILED
        msg = "SSH port 22 did not become available within 5 minutes after server was running."
        server.provisioning_log = _append_text(server.provisioning_log, msg)
        server.save(update_fields=["status", "provisioning_log", "updated_at"])
        _broadcast_server(server.id, msg, server.status, done=True)
        _job_done(job_id, ok=False, log=msg)
        return

    logger.info("Server %s: SSH is ready — starting Ansible.", server.id)

    admin_email = os.getenv("ODOO_ADMIN_EMAIL", "odoo@example.com").strip()
    extra_vars = {
        "odoo_version": server.odoo_version,
        "server_name": server.name,
        "dns_domain": server.dns_domain,
        "website_name": server.dns_domain if server.dns_domain else "_",
        "admin_email": admin_email,
    }

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
    if ok:
        # Ansible ran successfully over SSH — mark server as reachable so the
        # connectivity badge shows "Connected" and instance creation is unblocked.
        server.is_reachable = True
        server.last_checked_at = timezone.now()
        server.save(update_fields=["is_reachable", "last_checked_at", "updated_at"])
    if ok and server.deployment_mode == OdooServer.DeploymentMode.BARE_METAL and (
        server.domain_routing_enabled or server.managed_dns_enabled
    ):
        gateway_ok, gateway_log = _ensure_bare_traefik_gateway(server)
        server.provisioning_log = _append_text(server.provisioning_log, f"[traefik gateway]\n{gateway_log}".strip())
        if not gateway_ok:
            ok = False
            log_blob = _append_text(log_blob, gateway_log)

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

        # Pre-populate the server's Enterprise shared directory so the first
        # instance activation is just a fast local copy (no network upload needed).
        platform_source = EnterpriseSource.active_for_version(server.odoo_version)
        if platform_source and platform_source.source_scope == EnterpriseSource.Scope.PLATFORM:
            _broadcast_server(server.id, "Pre-syncing Enterprise addons to server shared directory…", server.status)
            ent_ok, ent_log = _sync_enterprise_to_server(
                server, platform_source,
                on_line=lambda line: _broadcast_log_line(ws_group, line),
            )
            server.provisioning_log = _append_text(server.provisioning_log, f"[enterprise pre-sync]\n{ent_log}".strip())
            server.save(update_fields=["provisioning_log", "updated_at"])
            if not ent_ok:
                logger.warning("Server %s: Enterprise pre-sync failed (non-fatal): %s", server.id, ent_log)

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
    try:
        _record_instance_step(
            instance,
            "Celery task accepted",
            f"instance_id={instance.id}\nserver_id={server.id}\ndeployment_mode={server.deployment_mode}\nserver_ip={server.ip_address or '-'}",
        )

        if server.status != OdooServer.Status.PROVISIONED or not server.ip_address:
            logger.error(
                "Instance %s: server not ready for instance creation (status=%s ip=%s)",
                instance.id,
                server.status,
                server.ip_address,
            )
            instance.status = OdooInstance.Status.FAILED
            msg = f"Server is not ready for instance creation. status={server.status} ip={server.ip_address or '-'}"
            _record_instance_error(instance, "Server readiness check failed", msg)
            instance.save(update_fields=["status", "provisioning_log", "updated_at"])
            _job_done(job_id, ok=False, log=msg)
            return

        instance.status = OdooInstance.Status.CONFIGURING
        instance.installation_summary = {}
        instance.installation_summary_text = ""
        instance.save(update_fields=["status", "installation_summary", "installation_summary_text", "updated_at"])
        _record_instance_step(instance, "Starting instance configuration")
        _broadcast_instance(instance.id, "Starting instance configuration…", instance.status)

        if instance.domain:
            try:
                _record_instance_step(instance, "Prewarming domain overlay", f"domain={instance.domain}")
                prewarm_ok, prewarm_message = _reconcile_instance_domain(instance, skip_probe=True)
                if prewarm_message:
                    if prewarm_ok:
                        _record_instance_step(instance, "Domain overlay queued", prewarm_message)
                    else:
                        _record_instance_error(instance, "Prewarm domain overlay failed", prewarm_message)
            except Exception as exc:
                logger.warning("Instance %s: prewarm domain overlay failed", instance.id, exc_info=True)
                _record_instance_error(instance, "Prewarm domain overlay failed", str(exc))

        if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
            _record_instance_step(instance, "Delegating to Docker deployment flow")
            _run_docker_instance_create(instance, server, job_id, self.request.id)
            return

        direct_playbook = os.getenv("ANSIBLE_ODOO_INSTANCE_DIRECT_PLAYBOOK", "").strip() or _default_odoo_instance_direct_playbook()
        use_direct = True
        playbook = direct_playbook

        if not Path(playbook).exists():
            logger.error("Instance %s: instance playbook not found: %s", instance.id, playbook)
            instance.status = OdooInstance.Status.FAILED
            msg = f"Instance playbook not found: {playbook}"
            _record_instance_error(instance, "Playbook lookup failed", msg)
            instance.save(update_fields=["status", "provisioning_log", "updated_at"])
            _job_done(job_id, ok=False, log=msg)
            return

        extra_vars = {
            "odoo_version": server.odoo_version,
            "db_name": instance.db_name,
            "instance_name": instance.name,
            "http_port": instance.http_port,
            "restart_policy": instance.restart_policy,
            "proxy_mode": bool(instance.domain),
        }

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
        _record_instance_step(
            instance,
            "Running instance playbook",
            f"playbook={playbook_name}\nserver_ip={server.ip_address}\nhttp_port={instance.http_port}\ndb_name={instance.db_name}\ndomain={instance.domain or '-'}",
        )
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
        domain_message = ""
        if ok:
            _record_instance_step(instance, "Instance playbook finished", "Base Odoo instance created; applying post-provision steps.")
            instance.status = OdooInstance.Status.RUNNING
            instance.systemd_service = f"odoo-{instance.db_name}"
            instance.nginx_site = ""
            instance.ssl_enabled = False
            if instance.domain:
                _record_instance_step(instance, "Reconciling domain overlay", f"domain={instance.domain}")
                domain_ok, domain_message = _reconcile_instance_domain(instance)
                if not domain_ok:
                    logger.warning("Instance %s: domain overlay failed: %s", instance.id, domain_message)
                    _record_instance_error(instance, "Domain reconciliation failed", domain_message)
                elif domain_message:
                    _record_instance_step(instance, "Domain reconciliation finished", domain_message)
            else:
                _save_instance_domain_state(
                    instance,
                    domain_status=OdooInstance.DomainStatus.NOT_CONFIGURED,
                    ssl_status=OdooInstance.SSLStatus.NOT_CONFIGURED,
                    ssl_enabled=False,
                    ssl_error="",
                    checked_at=timezone.now(),
                )
            _record_instance_step(instance, "Building installation summary")
            summary, summary_text = _store_instance_installation_summary(
                instance,
                server=server,
                playbook=playbook,
                ssh_user=ssh_user or "root",
                use_direct=use_direct,
            )
            # Store the admin user password extracted from playbook output
            admin_user_pw = _extract_odoo_admin_user_password(log_blob)
            if admin_user_pw:
                instance.odoo_admin_password = admin_user_pw
                instance.save(update_fields=["odoo_admin_password", "updated_at"])
            _initialize_instance_addons_metadata(instance)
            _record_instance_step(instance, "Addon metadata initialized")
        else:
            instance.status = OdooInstance.Status.FAILED
            _record_instance_error(instance, "Instance playbook failed", "Check the [ansible instance] section below for the full Ansible output.")
            reachable, message = _probe_server_ssh(server)
            if not reachable:
                _persist_server_reachability(server, reachable=False, message=message)
                _record_instance_error(instance, "Server reachability check failed", message)
        instance.provisioning_log = _append_text(
            instance.provisioning_log,
            "Instance created successfully — ready." if ok else "Instance creation failed.",
        )
        if domain_message:
            instance.provisioning_log = _append_text(instance.provisioning_log, domain_message)
        instance.save(
            update_fields=[
                "status",
                "systemd_service",
                "nginx_site",
                "ssl_enabled",
                "domain_status",
                "domain_last_checked_at",
                "ssl_status",
                "ssl_error",
                "provisioning_log",
                "addons_root_path",
                "addons_path_cache",
                "addons_sync_status",
                "addons_last_sync_at",
                "updated_at",
            ]
        )
        access_url = instance.access_url or (f"http://{server.ip_address}:{instance.http_port}" if server.ip_address else "")
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
    except Exception:
        error_log = traceback.format_exc()
        logger.exception("Instance %s: creation task crashed unexpectedly", instance.id)
        instance.status = OdooInstance.Status.FAILED
        _record_instance_error(instance, "Unexpected Celery task error", error_log)
        instance.save(update_fields=["status", "provisioning_log", "updated_at"])
        _broadcast_instance(
            instance.id,
            "Instance creation crashed.",
            instance.status,
            done=True,
            log=error_log,
        )
        _job_done(job_id, ok=False, log=error_log)


@shared_task(bind=True, max_retries=0)
def delete_odoo_instance(self, instance_id: int, job_id: int | None = None):
    """
    Stop the remote Odoo service, drop the database, clean up files,
    then remove the instance record from the database.

    Remote cleanup runs FIRST. The Django record is only deleted after the
    remote work succeeds (or after a best-effort attempt if it partially fails).
    The job (if supplied) is marked done/failed accordingly.
    """
    instance = OdooInstance.objects.select_related(
        "organization",
        "server",
        "server__infrastructure",
        "server__infrastructure__external_server",
    ).get(pk=instance_id)
    server = instance.server

    _job_start(job_id, self.request.id)
    full_log = ""
    ok = True

    try:
        # 1. Detach any active domain assignments first
        for assignment in _active_domain_assignments(instance):
            domain_ok, domain_log = _detach_instance_domain_overlay(instance, assignment.domain)
            if domain_log:
                full_log = _append_text(full_log, f"[domain detach]\n{domain_log}")
                instance.provisioning_log = _append_text(instance.provisioning_log, f"[domain detach]\n{domain_log}")
            if not domain_ok:
                logger.warning("Instance %s domain cleanup completed with warnings: %s", instance_id, domain_log)

        # 2. Run the appropriate remote cleanup
        if server.ip_address:
            if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
                _run_docker_instance_delete(instance, server)
            else:
                _run_bare_metal_instance_delete(instance, server)
            full_log = _append_text(full_log, instance.provisioning_log)
        else:
            msg = "Server IP unavailable; skipped remote cleanup."
            full_log = _append_text(full_log, msg)
            instance.provisioning_log = _append_text(instance.provisioning_log, msg)
            instance.status = OdooInstance.Status.DELETED
            instance.save(update_fields=["status", "provisioning_log", "updated_at"])

    except Exception as exc:
        ok = False
        err_msg = f"Remote cleanup error: {exc}"
        full_log = _append_text(full_log, err_msg)
        logger.warning("Instance %s cleanup failed; will still remove Django record.", instance_id, exc_info=True)

    finally:
        _job_done(job_id, ok=ok, log=full_log)
        _broadcast_instance_removed(instance.id, server.id)
        try:
            instance.delete()
        except Exception:
            logger.warning("Could not delete OdooInstance %s DB record.", instance_id, exc_info=True)
        _broadcast_server_snapshot(server)


@shared_task(bind=True, max_retries=0)
def delete_odoo_server(self, server_id: int, job_id: int | None = None):
    """
    Delete an OdooServer and all its instances.

    For each instance:
      - Best-effort remote cleanup (stop service / drop DB / remove container).
        SSH errors are logged and skipped — the instance DB record is deleted regardless.
    After all instances are gone the server DB record is deleted unconditionally.
    The job (if supplied) is marked done/failed accordingly.
    """
    server = OdooServer.objects.select_related(
        "organization",
        "infrastructure",
        "infrastructure__external_server",
    ).get(pk=server_id)

    _job_start(job_id, self.request.id)
    full_log = ""
    overall_ok = True

    # ── 1. Clean up each instance ────────────────────────────────────────────
    instances = list(server.instances.exclude(status=OdooInstance.Status.DELETED))
    for instance in instances:
        inst_log = f"[instance {instance.db_name}]\n"
        try:
            # Domain detach (best-effort)
            for assignment in _active_domain_assignments(instance):
                _, domain_log = _detach_instance_domain_overlay(instance, assignment.domain)
                if domain_log:
                    inst_log += domain_log + "\n"

            # Remote service/container cleanup (best-effort — skip if unreachable)
            if server.ip_address:
                if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
                    _run_docker_instance_delete(instance, server)
                else:
                    _run_bare_metal_instance_delete(instance, server)
                inst_log += "Remote cleanup completed.\n"
            else:
                inst_log += "No server IP — skipped remote cleanup.\n"

        except Exception as exc:
            overall_ok = False
            inst_log += f"Remote cleanup failed (SSH unreachable or error): {exc}\n"
            inst_log += "Removing Django record anyway.\n"
            logger.warning(
                "Server %s instance %s remote cleanup failed; removing record anyway.",
                server_id, instance.pk, exc_info=True,
            )

        # Always remove the Django instance record
        try:
            _broadcast_instance_removed(instance.id, server_id)
            instance.delete()
            inst_log += "Instance record deleted.\n"
        except Exception as exc:
            inst_log += f"Could not delete instance record: {exc}\n"
            logger.warning("Could not delete instance %s record.", instance.pk, exc_info=True)

        full_log = _append_text(full_log, inst_log)

    # ── 2. Delete the server DB record ───────────────────────────────────────
    full_log = _append_text(full_log, f"Deleting server record (id={server_id}).")
    try:
        server.delete()
        full_log = _append_text(full_log, "Server record deleted.")
    except Exception as exc:
        overall_ok = False
        full_log = _append_text(full_log, f"Could not delete server record: {exc}")
        logger.exception("Could not delete OdooServer %s record.", server_id)

    _job_done(job_id, ok=overall_ok, log=full_log)


@shared_task(bind=True, max_retries=0)
def provision_instance_domain(self, instance_id: int):
    instance = OdooInstance.objects.select_related("server").get(pk=instance_id)
    if instance.status == OdooInstance.Status.DELETED:
        return
    ok, message = _reconcile_instance_domain(instance)
    instance.provisioning_log = _append_text(instance.provisioning_log, f"[domain]\n{message}".strip())
    instance.save(update_fields=["provisioning_log", "updated_at"])
    _broadcast_instance(
        instance.id,
        message,
        instance.status,
        done=False,
    )
    if not ok:
        logger.warning("Instance %s domain provisioning failed: %s", instance.id, message)


@shared_task(bind=True, max_retries=0)
def detach_instance_domain(self, instance_id: int, domain: str | None = None):
    instance = OdooInstance.objects.select_related("server").get(pk=instance_id)
    if instance.status == OdooInstance.Status.DELETED:
        return
    ok, message = _detach_instance_domain_overlay(instance, domain)
    instance.provisioning_log = _append_text(instance.provisioning_log, f"[domain detach]\n{message}".strip())
    instance.save(update_fields=["provisioning_log", "updated_at"])
    _broadcast_instance(
        instance.id,
        "Domain detached." if ok else f"Domain detach completed with warnings: {message}",
        instance.status,
        done=False,
    )


@shared_task
def reconcile_instance_domains():
    instances = (
        OdooInstance.objects.filter(domain_assignments__status__in=[
            DomainAssignment.Status.PENDING,
            DomainAssignment.Status.ACTIVE,
            DomainAssignment.Status.FAILED,
        ])
        .exclude(status=OdooInstance.Status.DELETED)
        .select_related("server", "server__managed_dns_zone")
        .distinct()
    )
    for instance in instances:
        try:
            ok, message = _reconcile_instance_domain(instance)
            instance.provisioning_log = _append_text(instance.provisioning_log, f"[domain reconcile]\n{message}".strip())
            instance.save(update_fields=["provisioning_log", "updated_at"])
            if not ok:
                logger.warning("Instance %s domain reconciliation failed: %s", instance.id, message)
        except Exception:
            logger.warning("Instance %s domain reconciliation crashed.", instance.id, exc_info=True)


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


def _probe_server_ssh(server: OdooServer, timeout: int = 15) -> tuple[bool, str]:
    """Validate SSH reachability using ansible-playbook with a minimal gather-facts play."""
    host, port = _odoo_server_ssh_target(server)
    if not host:
        return False, "No host/IP to probe — server has no SSH target yet."

    ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
    if not (ssh_key or ssh_password):
        return False, f"No SSH credentials available for {host}:{port}."

    effective_user = ssh_user or os.getenv("ANSIBLE_SSH_USER", "root").strip()
    message = ""
    tmp_playbook = None
    try:
        tmp_playbook = tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False)
        tmp_playbook.write(
            "- hosts: all\n"
            "  gather_facts: true\n"
            "  tasks:\n"
            "    - name: Connectivity OK\n"
            "      ansible.builtin.debug:\n"
            "        msg: reachability-ok\n"
        )
        tmp_playbook.close()

        args = [
            "ansible-playbook",
            tmp_playbook.name,
            "-i",
            f"{host},",
            "--user",
            effective_user,
            "--ssh-extra-args",
            f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout={timeout}",
            "-e",
            f"ansible_port={port}",
        ]
        if ssh_key:
            args.extend(["--private-key", ssh_key])
        if ssh_password and not ssh_key:
            args.extend(["-e", f"ansible_ssh_pass={ssh_password}"])
            args.extend(["-e", f"ansible_password={ssh_password}"])

        code, out, err = _run_cmd(
            args,
            Path(settings.BASE_DIR),
            extra_env={"ANSIBLE_HOST_KEY_CHECKING": "False"},
        )
        log_blob = _append_text(out.strip(), err.strip())
        if code == 0:
            return True, f"SSH validation succeeded for {host}:{port}."
        message = _extract_ansible_unreachable_message(log_blob)
        if not message:
            non_empty = [line.strip() for line in log_blob.splitlines() if line.strip()]
            message = non_empty[-1] if non_empty else f"SSH validation failed for {host}:{port}."
        return False, message
    except FileNotFoundError:
        return False, "Ansible command not found for reachability check."
    except Exception as exc:
        return False, f"SSH validation failed for {host}:{port}: {exc}"
    finally:
        if tmp_key:
            with suppress(OSError):
                os.unlink(tmp_key)
        if tmp_playbook:
            with suppress(OSError):
                os.unlink(tmp_playbook.name)


def _persist_server_reachability(
    server: OdooServer,
    *,
    reachable: bool,
    message: str = "",
    checked_at=None,
    broadcast: bool = True,
):
    now = checked_at or timezone.now()
    server.is_reachable = reachable
    server.last_checked_at = now
    infra = getattr(server, "infrastructure", None)
    update_fields = ["is_reachable", "last_checked_at", "updated_at"]

    if (
        not reachable
        and infra
        and infra.infra_type == Infrastructure.InfraType.PYOS
        and server.status in (
            OdooServer.Status.CONNECTING,
            OdooServer.Status.PROVISIONING,
            OdooServer.Status.CONFIGURING,
        )
    ):
        server.status = OdooServer.Status.FAILED
        update_fields.append("status")
        if message:
            server.provisioning_log = _append_text(server.provisioning_log, message)
            update_fields.append("provisioning_log")
        if server.celery_task_id:
            server.celery_task_id = ""
            update_fields.append("celery_task_id")

    server.save(update_fields=update_fields)

    if infra and infra.infra_type == Infrastructure.InfraType.PYOS and infra.external_server:
        ext = infra.external_server
        ext.is_reachable = reachable
        ext.last_checked_at = now
        ext.is_verified = reachable
        ext.verification_error = "" if reachable else message
        ext.last_verified_at = now
        ext.save(
            update_fields=[
                "is_reachable",
                "last_checked_at",
                "is_verified",
                "verification_error",
                "last_verified_at",
            ]
        )

    if broadcast:
        _broadcast_server_snapshot(server)


def _extract_ansible_unreachable_message(log_blob: str) -> str:
    for raw_line in reversed((log_blob or "").splitlines()):
        line = raw_line.strip()
        lowered = line.lower()
        if not line:
            continue
        if "unreachable!" in lowered or "failed to connect to the host via ssh" in lowered:
            return line
    return ""


def _mark_server_unreachable_from_ansible_log(server: OdooServer, log_blob: str) -> bool:
    message = _extract_ansible_unreachable_message(log_blob)
    if not message:
        return False
    _persist_server_reachability(server, reachable=False, message=message)
    return True


@shared_task(bind=True, max_retries=0)
def refresh_traefik_gateway(self, server_id: int):
    """
    Force-refresh the Traefik gateway on a bare-metal server.
    Clears the 5-minute cache lock so the gateway playbook runs immediately,
    re-deploying the static config (and restarting Traefik via the handler
    when the config actually changed).
    """
    server = OdooServer.objects.filter(pk=server_id, is_active=True).first()
    if not server:
        logger.warning("refresh_traefik_gateway: server %s not found.", server_id)
        return
    if server.deployment_mode != OdooServer.DeploymentMode.BARE_METAL:
        logger.info("refresh_traefik_gateway: server %s is not bare-metal, skipping.", server_id)
        return
    # Clear the gateway cache so _ensure_bare_traefik_gateway actually runs the playbook.
    cache_key = f"deployments:server:{server_id}:traefik-gateway-ready"
    cache.delete(cache_key)
    # Remove stale _acme-challenge TXT records from Cloudflare before resetting ACME.
    # Stale records cause error 81058 ("identical record already exists") on the next
    # cert request, silently blocking certificate issuance.
    _cleanup_acme_challenge_dns_records(
        base_domain=os.getenv("PLATFORM_BASE_DOMAIN", "").strip(),
        cf_token=os.getenv("PLATFORM_DNS_API_TOKEN", "").strip(),
    )
    ok, log_blob = _ensure_bare_traefik_gateway(server, acme_reset=True)
    logger.info(
        "refresh_traefik_gateway: server %s — ok=%s log=%s",
        server_id, ok, log_blob[:200],
    )


@shared_task
def check_server_connectivity():
    """
    Periodic task: SSH-validate every active OdooServer and ExternalServer.
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
        reachable, message = _probe_server_ssh(server)
        if server.ip_address != host:
            server.ip_address = host
            server.save(update_fields=["ip_address", "updated_at"])
        _persist_server_reachability(server, reachable=reachable, message=message, checked_at=now)
        logger.info(
            "Reachability check: server %s (%s:%s) is %s (%s)",
            server.id,
            host,
            port,
            "connected" if reachable else "disconnected",
            message,
        )

    # --- ExternalServer: validate SSH using the same PYOS connection path ---
    ext_servers = ExternalServer.objects.filter(host__isnull=False)

    for ext in ext_servers:
        from cloud.pyos import PyOSService

        reachable, message = PyOSService(ext).validate()
        ext.is_reachable = reachable
        ext.last_checked_at = now
        ext.is_verified = reachable
        ext.verification_error = "" if reachable else message
        ext.last_verified_at = now
        ext.save(
            update_fields=[
                "is_reachable",
                "last_checked_at",
                "is_verified",
                "verification_error",
                "last_verified_at",
            ]
        )
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
    if ok:
        _initialize_instance_addons_metadata(instance)
    instance.save(
        update_fields=[
            "status",
            "provisioning_log",
            "addons_root_path",
            "addons_path_cache",
            "addons_sync_status",
            "addons_last_sync_at",
            "updated_at",
        ]
    )
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
# Git addon manager tasks
# ---------------------------------------------------------------------------

def _repo_task_context(repo_id: int) -> OdooInstanceGitRepo:
    return OdooInstanceGitRepo.objects.select_related(
        "instance",
        "instance__organization",
        "instance__server",
        "instance__server__infrastructure",
        "instance__server__infrastructure__external_server",
        "credential",
        "credential__github_account",
    ).get(pk=repo_id)


def _repo_follow_up_success(
    repo: OdooInstanceGitRepo,
    *,
    log_blob: str,
    modules: list[str] | None = None,
    allow_refresh_failure: bool = False,
    on_line=None,
):
    _append_repo_log(repo, log_blob, reset=False)
    sync_log = _sync_instance_addons_config(repo.instance, on_line=on_line)
    requirements_log = ""
    if repo.install_requirements_on_update:
        requirements_log = _install_repo_python_requirements(repo, on_line=on_line)
    refresh_log = _restart_and_refresh_instance_addons(
        repo.instance,
        modules=modules if repo.auto_upgrade_modules_on_update else None,
        allow_refresh_failure=allow_refresh_failure,
        on_line=on_line,
    )
    _append_repo_log(repo, sync_log)
    if requirements_log:
        _append_repo_log(repo, requirements_log)
    _append_repo_log(repo, refresh_log)
    repo.last_error = ""
    repo.status = OdooInstanceGitRepo.Status.CONNECTED
    repo.last_sync_finished_at = timezone.now()
    repo.save(
        update_fields=[
            "last_sync_log",
            "last_error",
            "status",
            "last_sync_finished_at",
            "updated_at",
        ]
    )
    _broadcast_repo_event(
        repo.instance_id,
        {
            "type": "repo.synced",
            "repo_id": repo.id,
            "status": repo.status,
            "modules": modules or [],
        },
    )
    log_audit(
        repo.created_by,
        AuditLog.Action.OTHER,
        None,
        f"Repo '{repo.repo_name}' synced for instance '{repo.instance.name}'.",
        metadata={
            "instance_id": repo.instance_id,
            "repo_id": repo.id,
            "modules": modules or [],
        },
        organization=repo.instance.organization,
    )
    return _append_text(log_blob, _append_text(sync_log, refresh_log))


def _repo_mark_error(repo: OdooInstanceGitRepo, message: str, job_id: int | None = None):
    repo.instance.addons_sync_status = OdooInstance.AddonsSyncStatus.ERROR
    repo.instance.addons_last_sync_at = timezone.now()
    repo.instance.save(update_fields=["addons_sync_status", "addons_last_sync_at", "updated_at"])
    _set_repo_status(
        repo,
        status=OdooInstanceGitRepo.Status.ERROR,
        last_error=message,
        append_log=message,
        finished=True,
    )
    _job_done(job_id, ok=False, log=message)


def _queue_auto_update_for_repo(repo: OdooInstanceGitRepo) -> bool:
    if not repo.auto_update or not repo.is_enabled or repo.pinned_commit:
        return False
    if repo.instance.status != OdooInstance.Status.RUNNING:
        return False
    if repo.status in (OdooInstanceGitRepo.Status.CLONING, OdooInstanceGitRepo.Status.UPDATING):
        return False
    if DeploymentJob.objects.filter(
        odoo_instance=repo.instance,
        job_type=DeploymentJob.JobType.AUTO_SYNC_INSTANCE_REPOS,
        status__in=[DeploymentJob.Status.QUEUED, DeploymentJob.Status.RUNNING],
    ).exists():
        return False

    job = DeploymentJob.objects.create(
        organization=repo.instance.organization,
        job_type=DeploymentJob.JobType.AUTO_SYNC_INSTANCE_REPOS,
        odoo_instance=repo.instance,
        created_by=repo.created_by or repo.instance.created_by,
    )
    repo.status = OdooInstanceGitRepo.Status.UPDATING
    repo.last_error = ""
    repo.save(update_fields=["status", "last_error", "updated_at"])
    try:
        update_instance_repo.delay(repo.id, job.id)
    except Exception:
        update_instance_repo(repo.id, job.id)
    return True


@shared_task(bind=True, max_retries=0)
def refresh_instance_addons(self, instance_id: int, job_id: int | None = None):
    instance = OdooInstance.objects.select_related(
        "organization",
        "server",
        "server__infrastructure",
        "server__infrastructure__external_server",
    ).get(pk=instance_id)

    _job_start(job_id, self.request.id)
    lock_token = _acquire_repo_lock(instance.id)
    try:
        instance.addons_sync_status = OdooInstance.AddonsSyncStatus.PENDING
        instance.save(update_fields=["addons_sync_status", "updated_at"])
        log_blob = _sync_instance_addons_config(instance)
        log_blob = _append_text(log_blob, _restart_and_refresh_instance_addons(instance))
        instance.addons_sync_status = OdooInstance.AddonsSyncStatus.READY
        instance.addons_last_sync_at = timezone.now()
        instance.save(update_fields=["addons_sync_status", "addons_last_sync_at", "updated_at"])
        _job_done(job_id, ok=True, log=log_blob)
    except Exception as exc:
        logger.exception("Instance %s addon refresh failed", instance.id)
        instance.addons_sync_status = OdooInstance.AddonsSyncStatus.ERROR
        instance.addons_last_sync_at = timezone.now()
        instance.save(update_fields=["addons_sync_status", "addons_last_sync_at", "updated_at"])
        _job_done(job_id, ok=False, log=str(exc))
        raise
    finally:
        _release_repo_lock(instance.id, lock_token)


@shared_task(bind=True, max_retries=0)
def activate_enterprise_for_instance(self, instance_id: int, source_id: int | None = None, job_id: int | None = None):
    """
    Enterprise activation flow:

    PLATFORM source
    ---------------
    1. Check if server already has this release in enterprise_shared_path.
       - If not (or outdated): upload from DafeApp host → server shared dir  [network, once per server]
       - If yes: skip upload entirely.
    2. Local copy on server: shared dir → instance enterprise path            [disk only, fast]
    3. Rewrite addons_path in instance config to include the enterprise path.
    4. Restart service + refresh module list.

    USER source
    -----------
    Same flow but skips the server-level shared dir — copies directly from the
    DafeApp host to the instance path (user sources are per-user, not shared).
    """
    instance = OdooInstance.objects.select_related(
        "organization",
        "server",
        "enterprise_source",
    ).get(pk=instance_id)
    server = instance.server

    _job_start(job_id, self.request.id)
    lock_token = _acquire_repo_lock(instance.id)
    ws_group = f"odoo.instance.{instance.id}"
    source = None
    full_log = ""

    try:
        # ── Resolve source ──────────────────────────────────────────────────
        if source_id:
            source = EnterpriseSource.objects.filter(pk=source_id, status=EnterpriseSource.Status.READY).first()
        if source is None:
            source = EnterpriseSource.active_for_version(server.odoo_version)
        if source is None:
            raise RuntimeError(f"No active Enterprise source is ready for Odoo {server.odoo_version}.")
        if not source.addons_source_path or not Path(source.addons_source_path).exists():
            raise RuntimeError("The Enterprise source package is missing from the DafeApp filesystem.")

        instance_path = _enterprise_host_local_path(instance)
        instance.enterprise_status = OdooInstance.EnterpriseStatus.PENDING
        instance.enterprise_error = ""
        instance.save(update_fields=["enterprise_status", "enterprise_error", "updated_at"])

        is_platform = source.source_scope == EnterpriseSource.Scope.PLATFORM
        is_docker = server.deployment_mode == OdooServer.DeploymentMode.DOCKER

        if is_docker:
            # ── Docker: sync addons to server, then bind-mount into container ────
            _broadcast_instance(instance.id, "Checking server Enterprise directory…", instance.status)
            _broadcast_log_line(ws_group, "=== Step 1/3: Sync Enterprise to server ===")
            server_ok, server_log = _sync_enterprise_to_server(
                server, source,
                on_line=lambda line: _broadcast_log_line(ws_group, line),
            )
            full_log = _append_text(full_log, server_log)
            if not server_ok:
                raise RuntimeError(server_log or "Failed to sync Enterprise to server.")

            enterprise_host_path = _docker_instance_enterprise_host_path(instance, server, source)
            instance_path = enterprise_host_path  # stored in enterprise_remote_path

            _broadcast_log_line(ws_group, "=== Step 2/3: Mount Enterprise volume into container ===")
            ent_playbook = _default_docker_enterprise_update_playbook()
            if not Path(ent_playbook).exists():
                raise RuntimeError(f"Docker enterprise update playbook not found: {ent_playbook}")
            client_name = instance.db_name.replace("_", "-")
            ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
            try:
                ok, mount_log = _run_ansible_playbook(
                    ent_playbook,
                    str(server.ip_address),
                    {
                        "client_name": client_name,
                        "domain": instance.domain or "",
                        "db_name": instance.db_name,
                        "odoo_version": server.odoo_version,
                        "postgres_password": server.docker_postgres_password,
                        "restart_policy": instance.restart_policy or "unless-stopped",
                        "container_name": instance.container_name or f"odoo-{client_name}",
                        "http_port": instance.http_port,
                        "cpu_limit": float(instance.requested_cpu_cores or 1),
                        "mem_limit_mb": int(instance.requested_ram_mb or 1024),
                        "enterprise_addons_host_path": enterprise_host_path,
                    },
                    ssh_user=ssh_user,
                    ssh_key_path=ssh_key,
                    ssh_password=ssh_password,
                    on_chunk=lambda line: _broadcast_log_line(ws_group, line),
                )
            finally:
                if tmp_key:
                    with suppress(OSError):
                        os.unlink(tmp_key)
            full_log = _append_text(full_log, mount_log)
            if not ok:
                raise RuntimeError(mount_log or "Failed to mount Enterprise volume into container.")

        elif is_platform:
            # ── Step 1: Ensure server shared dir is up-to-date ──────────────
            _broadcast_instance(instance.id, "Checking server Enterprise shared directory…", instance.status)
            _broadcast_log_line(ws_group, "=== Step 1/3: Sync Enterprise to server shared directory ===")
            server_ok, server_log = _sync_enterprise_to_server(
                server, source,
                on_line=lambda line: _broadcast_log_line(ws_group, line),
            )
            full_log = _append_text(full_log, server_log)
            if not server_ok:
                raise RuntimeError(server_log or "Failed to sync Enterprise package to server shared directory.")

            # ── Step 2: Local copy shared dir → instance path ────────────────
            _broadcast_log_line(ws_group, "=== Step 2/3: Copy Enterprise from server shared dir to instance ===")
            shared_src = _server_enterprise_shared_path(server)
            playbook = _default_enterprise_sync_playbook()
            if not Path(playbook).exists():
                raise RuntimeError(f"Instance enterprise copy playbook not found: {playbook}")
            ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
            try:
                ok, copy_log = _run_ansible_playbook(
                    playbook,
                    str(server.ip_address),
                    {"enterprise_src": shared_src, "enterprise_dest": instance_path, "odoo_user": "odoo"},
                    ssh_user=ssh_user,
                    ssh_key_path=ssh_key,
                    ssh_password=ssh_password,
                    on_chunk=lambda line: _broadcast_log_line(ws_group, line),
                )
            finally:
                if tmp_key:
                    with suppress(OSError):
                        os.unlink(tmp_key)
            full_log = _append_text(full_log, copy_log)
            if not ok:
                raise RuntimeError(copy_log or "Local copy of Enterprise addons to instance path failed.")

        else:
            # ── USER source: direct DafeApp host → instance path ────────────
            _broadcast_log_line(ws_group, "=== Step 1/2: Upload user Enterprise source to instance ===")
            playbook = _default_enterprise_sync_playbook()
            # For user sources reuse the same instance-copy playbook but point
            # enterprise_src at the DafeApp-local addons path — Ansible will
            # use its own synchronize (network transfer) since src is local.
            # We swap back to the server-sync playbook which handles DafeApp→server.
            upload_playbook = _default_enterprise_server_sync_playbook()
            if not Path(upload_playbook).exists():
                raise RuntimeError(f"Enterprise upload playbook not found: {upload_playbook}")
            ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(server)
            try:
                ok, upload_log = _run_ansible_playbook(
                    upload_playbook,
                    str(server.ip_address),
                    {"enterprise_src": source.addons_source_path, "enterprise_dest": instance_path, "odoo_user": "odoo"},
                    ssh_user=ssh_user,
                    ssh_key_path=ssh_key,
                    ssh_password=ssh_password,
                    on_chunk=lambda line: _broadcast_log_line(ws_group, line),
                )
            finally:
                if tmp_key:
                    with suppress(OSError):
                        os.unlink(tmp_key)
            full_log = _append_text(full_log, upload_log)
            if not ok:
                raise RuntimeError(upload_log or "Failed to upload user Enterprise source to instance.")

        # ── Step 3 (all paths): config + full module upgrade + restart ───────
        step_label = "3/3" if (is_platform or is_docker) else "2/2"
        _broadcast_log_line(ws_group, f"=== Step {step_label}: Update config, upgrade modules, restart ===")

        instance.enterprise_enabled = True
        instance.enterprise_source = source
        instance.enterprise_remote_path = instance_path
        instance.enterprise_status = OdooInstance.EnterpriseStatus.ACTIVE
        instance.enterprise_source_mode = source.source_scope
        instance.enterprise_error = ""
        instance.enterprise_last_synced_at = timezone.now()
        instance.save(
            update_fields=[
                "enterprise_enabled",
                "enterprise_source",
                "enterprise_source_mode",
                "enterprise_remote_path",
                "enterprise_status",
                "enterprise_error",
                "enterprise_last_synced_at",
                "updated_at",
            ]
        )

        sync_log = _sync_instance_addons_config(instance, on_line=lambda line: _broadcast_log_line(ws_group, line))
        _broadcast_log_line(ws_group, "=== Rebuilding installed modules and assets (-u all) ===")
        upgrade_log = _upgrade_all_instance_modules_once(instance, on_line=lambda line: _broadcast_log_line(ws_group, line))
        _broadcast_log_line(ws_group, "=== Restarting service and refreshing module registry ===")
        refresh_log = _restart_and_refresh_instance_addons(instance, on_line=lambda line: _broadcast_log_line(ws_group, line))
        full_log = _append_text(full_log, _append_text(sync_log, _append_text(upgrade_log, refresh_log)))

        _broadcast_instance(
            instance.id,
            "Enterprise addons activated.",
            instance.status,
            summary={
                "enterprise_enabled": True,
                "enterprise_status": OdooInstance.EnterpriseStatus.ACTIVE,
                "enterprise_source": source.package_name,
                "enterprise_source_mode": instance.enterprise_source_mode,
                "enterprise_remote_path": instance.enterprise_remote_path,
                "enterprise_error": "",
            },
        )
        _job_done(job_id, ok=True, log=full_log)

    except Exception as exc:
        logger.exception("Enterprise activation failed for instance %s", instance.id)
        instance.enterprise_status = OdooInstance.EnterpriseStatus.ERROR
        instance.enterprise_error = str(exc)
        instance.save(update_fields=["enterprise_status", "enterprise_error", "updated_at"])
        _broadcast_instance(
            instance.id,
            "Enterprise activation failed.",
            instance.status,
            summary={
                "enterprise_enabled": instance.enterprise_enabled,
                "enterprise_status": OdooInstance.EnterpriseStatus.ERROR,
                "enterprise_error": str(exc),
                "enterprise_source": (
                    source.package_name
                    if source is not None
                    else (instance.enterprise_source.package_name if instance.enterprise_source_id else "")
                ),
                "enterprise_remote_path": instance.enterprise_remote_path,
            },
        )
        _job_done(job_id, ok=False, log=_append_text(full_log, str(exc)))
    finally:
        _release_repo_lock(instance.id, lock_token)


@shared_task(bind=True, max_retries=0)
def clone_instance_repo(self, repo_id: int, job_id: int | None = None):
    repo = _repo_task_context(repo_id)
    instance = repo.instance
    server = instance.server
    _job_start(job_id, self.request.id)

    try:
        lock_token = _acquire_repo_lock(instance.id)
    except RuntimeError as exc:
        _repo_mark_error(repo, str(exc), job_id=job_id)
        return

    ws_group = f"odoo.instance.{instance.id}"
    try:
        _update_repo_paths(instance, repo)
        repo.instance.addons_sync_status = OdooInstance.AddonsSyncStatus.PENDING
        repo.instance.save(update_fields=["addons_sync_status", "updated_at"])
        _set_repo_status(
            repo,
            status=OdooInstanceGitRepo.Status.CLONING,
            last_error="",
            append_log=f"Preparing clone for {repo.repo_name} ({repo.branch})…",
            reset_log=True,
            started=True,
        )
        new_commit, log_blob = _clean_clone_instance_repo(
            server,
            repo,
            repo.branch,
            on_line=lambda line: _broadcast_log_line(ws_group, line),
        )
        repo.last_pulled_commit = new_commit
        repo.previous_commit = ""
        repo.last_remote_commit = repo.last_pulled_commit
        repo.default_branch = repo.default_branch or repo.branch
        repo.last_pulled_at = timezone.now()
        repo.last_detected_modules = _detect_repo_modules(server, repo.local_path)
        if repo.credential_id:
            repo.credential.last_used_at = timezone.now()
            repo.credential.save(update_fields=["last_used_at", "updated_at"])
        repo.save(
            update_fields=[
                "last_pulled_commit",
                "previous_commit",
                "last_remote_commit",
                "default_branch",
                "last_pulled_at",
                "last_detected_modules",
                "updated_at",
            ]
        )

        full_log = _repo_follow_up_success(
            repo,
            log_blob=log_blob,
            modules=None,
            allow_refresh_failure=True,
            on_line=lambda line: _broadcast_log_line(ws_group, line),
        )
        _job_done(job_id, ok=True, log=full_log)
    except Exception as exc:
        logger.exception("Repo clone failed for repo %s", repo.id)
        _repo_mark_error(repo, str(exc), job_id=job_id)
    finally:
        _release_repo_lock(instance.id, lock_token)


@shared_task(bind=True, max_retries=0)
def update_instance_repo(self, repo_id: int, job_id: int | None = None, *, force_modules: bool = False):
    repo = _repo_task_context(repo_id)
    instance = repo.instance
    server = instance.server
    _job_start(job_id, self.request.id)

    try:
        lock_token = _acquire_repo_lock(instance.id)
    except RuntimeError as exc:
        _repo_mark_error(repo, str(exc), job_id=job_id)
        return

    ws_group = f"odoo.instance.{instance.id}"
    try:
        if repo.pinned_commit and not force_modules:
            raise RuntimeError("This repo is pinned to a specific commit. Clear the pin before updating.")

        _update_repo_paths(instance, repo)
        _set_repo_status(
            repo,
            status=OdooInstanceGitRepo.Status.UPDATING,
            last_error="",
            append_log=f"Checking remote changes for {repo.repo_name}:{repo.branch}…",
            reset_log=True,
            started=True,
        )
        repo.instance.addons_sync_status = OdooInstance.AddonsSyncStatus.PENDING
        repo.instance.save(update_fields=["addons_sync_status", "updated_at"])
        local_commit = _local_repo_head_commit(server, repo.local_path) or repo.last_pulled_commit or ""
        remote_commit, remote_log = _remote_repo_head_commit(
            server,
            repo,
            repo.branch,
            on_line=lambda line: _broadcast_log_line(ws_group, line),
        )
        repo.last_remote_commit = remote_commit
        repo.save(update_fields=["last_remote_commit", "updated_at"])
        if local_commit == remote_commit and _local_repo_head_commit(server, repo.local_path):
            repo.status = OdooInstanceGitRepo.Status.CONNECTED
            repo.last_sync_finished_at = timezone.now()
            repo.last_error = ""
            _append_repo_log(repo, _append_text(remote_log, "Already up to date."), reset=True)
            repo.save(
                update_fields=[
                    "status",
                    "last_remote_commit",
                    "last_sync_finished_at",
                    "last_error",
                    "last_sync_log",
                    "updated_at",
                ]
            )
            repo.instance.addons_sync_status = OdooInstance.AddonsSyncStatus.READY
            repo.instance.addons_last_sync_at = timezone.now()
            repo.instance.save(update_fields=["addons_sync_status", "addons_last_sync_at", "updated_at"])
            _job_done(job_id, ok=True, log=repo.last_sync_log)
            return

        reclone_notice = (
            f"Replacing local copy of {repo.repo_name}:{repo.branch} with a clean clone from GitHub…"
        )
        new_commit, clone_log = _clean_clone_instance_repo(
            server,
            repo,
            repo.branch,
            on_line=lambda line: _broadcast_log_line(ws_group, line),
        )
        changed_modules = _detect_changed_modules(server, repo.local_path, local_commit, new_commit)
        repo.previous_commit = local_commit
        repo.last_pulled_commit = new_commit
        repo.last_pulled_at = timezone.now()
        repo.last_detected_modules = changed_modules or _detect_repo_modules(server, repo.local_path)
        repo.save(
            update_fields=[
                "previous_commit",
                "last_remote_commit",
                "last_pulled_commit",
                "last_pulled_at",
                "last_detected_modules",
                "updated_at",
            ]
        )
        full_log = _repo_follow_up_success(
            repo,
            log_blob=_append_text(_append_text(remote_log, reclone_notice), clone_log),
            modules=changed_modules,
            on_line=lambda line: _broadcast_log_line(ws_group, line),
        )
        _job_done(job_id, ok=True, log=full_log)
    except Exception as exc:
        logger.exception("Repo update failed for repo %s", repo.id)
        _repo_mark_error(repo, str(exc), job_id=job_id)
    finally:
        _release_repo_lock(instance.id, lock_token)


@shared_task(bind=True, max_retries=0)
def checkout_instance_repo_branch(self, repo_id: int, branch: str, job_id: int | None = None):
    repo = _repo_task_context(repo_id)
    instance = repo.instance
    server = instance.server
    _job_start(job_id, self.request.id)

    try:
        lock_token = _acquire_repo_lock(instance.id)
    except RuntimeError as exc:
        _repo_mark_error(repo, str(exc), job_id=job_id)
        return

    ws_group = f"odoo.instance.{instance.id}"
    try:
        branch = (branch or "").strip()
        if not branch:
            raise RuntimeError("A target branch is required.")
        _update_repo_paths(instance, repo)

        _set_repo_status(
            repo,
            status=OdooInstanceGitRepo.Status.UPDATING,
            last_error="",
            append_log=f"Switching {repo.repo_name} to branch '{branch}'…",
            reset_log=True,
            started=True,
        )
        repo.instance.addons_sync_status = OdooInstance.AddonsSyncStatus.PENDING
        repo.instance.save(update_fields=["addons_sync_status", "updated_at"])

        previous_commit = repo.last_pulled_commit or ""
        remote_commit, remote_log = _remote_repo_head_commit(
            server,
            repo,
            branch,
            on_line=lambda line: _broadcast_log_line(ws_group, line),
        )
        reclone_notice = f"Replacing local copy with a clean clone of branch '{branch}'…"
        new_commit, clone_log = _clean_clone_instance_repo(
            server,
            repo,
            branch,
            on_line=lambda line: _broadcast_log_line(ws_group, line),
        )
        changed_modules = _detect_changed_modules(server, repo.local_path, previous_commit, new_commit)
        repo.branch = branch
        repo.previous_commit = previous_commit
        repo.last_pulled_commit = new_commit
        repo.last_remote_commit = remote_commit
        repo.pinned_commit = ""
        repo.last_pulled_at = timezone.now()
        repo.last_detected_modules = changed_modules or _detect_repo_modules(server, repo.local_path)
        repo.save(
            update_fields=[
                "branch",
                "previous_commit",
                "last_pulled_commit",
                "last_remote_commit",
                "pinned_commit",
                "last_pulled_at",
                "last_detected_modules",
                "updated_at",
            ]
        )
        full_log = _repo_follow_up_success(
            repo,
            log_blob=_append_text(_append_text(remote_log, reclone_notice), clone_log),
            modules=changed_modules,
            on_line=lambda line: _broadcast_log_line(ws_group, line),
        )
        _job_done(job_id, ok=True, log=full_log)
    except Exception as exc:
        logger.exception("Repo branch switch failed for repo %s", repo.id)
        _repo_mark_error(repo, str(exc), job_id=job_id)
    finally:
        _release_repo_lock(instance.id, lock_token)


@shared_task(bind=True, max_retries=0)
def rollback_instance_repo(self, repo_id: int, target_commit: str, job_id: int | None = None):
    repo = _repo_task_context(repo_id)
    instance = repo.instance
    server = instance.server
    _job_start(job_id, self.request.id)

    try:
        lock_token = _acquire_repo_lock(instance.id)
    except RuntimeError as exc:
        _repo_mark_error(repo, str(exc), job_id=job_id)
        return

    ws_group = f"odoo.instance.{instance.id}"
    try:
        target_commit = (target_commit or repo.previous_commit or "").strip()
        if not target_commit:
            raise RuntimeError("No previous commit is available for rollback.")

        _set_repo_status(
            repo,
            status=OdooInstanceGitRepo.Status.UPDATING,
            last_error="",
            append_log=f"Rolling {repo.repo_name} back to {target_commit}…",
            reset_log=True,
            started=True,
        )
        repo.instance.addons_sync_status = OdooInstance.AddonsSyncStatus.PENDING
        repo.instance.save(update_fields=["addons_sync_status", "updated_at"])

        code, output = _ssh_run(
            server,
            (
                f"cd {shlex.quote(repo.local_path)}"
                f" && git fetch --all --tags"
                f" && git checkout {shlex.quote(target_commit)}"
                f" && git rev-parse HEAD"
            ),
            on_line=lambda line: _broadcast_log_line(ws_group, line),
        )
        if code != 0:
            raise RuntimeError(output or "Repo rollback failed.")

        new_commit = output.splitlines()[-1].strip()
        previous_commit = repo.last_pulled_commit
        changed_modules = _detect_changed_modules(server, repo.local_path, previous_commit, new_commit)
        repo.previous_commit = previous_commit
        repo.last_pulled_commit = new_commit
        repo.last_remote_commit = new_commit
        repo.pinned_commit = new_commit
        repo.last_pulled_at = timezone.now()
        repo.last_detected_modules = changed_modules or _detect_repo_modules(server, repo.local_path)
        repo.save(
            update_fields=[
                "previous_commit",
                "last_pulled_commit",
                "last_remote_commit",
                "pinned_commit",
                "last_pulled_at",
                "last_detected_modules",
                "updated_at",
            ]
        )
        full_log = _repo_follow_up_success(
            repo,
            log_blob=output,
            modules=changed_modules,
            on_line=lambda line: _broadcast_log_line(ws_group, line),
        )
        _job_done(job_id, ok=True, log=full_log)
    except Exception as exc:
        logger.exception("Repo rollback failed for repo %s", repo.id)
        _repo_mark_error(repo, str(exc), job_id=job_id)
    finally:
        _release_repo_lock(instance.id, lock_token)


@shared_task(bind=True, max_retries=0)
def remove_instance_repo(self, repo_id: int, job_id: int | None = None):
    repo = _repo_task_context(repo_id)
    instance = repo.instance
    server = instance.server
    repo_label = repo.repo_name
    _job_start(job_id, self.request.id)

    try:
        lock_token = _acquire_repo_lock(instance.id)
    except RuntimeError as exc:
        _repo_mark_error(repo, str(exc), job_id=job_id)
        return

    ws_group = f"odoo.instance.{instance.id}"
    try:
        _set_repo_status(
            repo,
            status=OdooInstanceGitRepo.Status.UPDATING,
            last_error="",
            append_log=f"Removing repo {repo.repo_name}…",
            reset_log=True,
            started=True,
        )
        repo.instance.addons_sync_status = OdooInstance.AddonsSyncStatus.PENDING
        repo.instance.save(update_fields=["addons_sync_status", "updated_at"])

        code, output = _ssh_run(
            server,
            f"rm -rf {shlex.quote(repo.local_path)}",
            on_line=lambda line: _broadcast_log_line(ws_group, line),
        )
        if code != 0:
            raise RuntimeError(output or "Remote repo removal failed.")

        instance_id = instance.id
        repo.delete()
        sync_log = _sync_instance_addons_config(instance, on_line=lambda line: _broadcast_log_line(ws_group, line))
        refresh_log = _restart_and_refresh_instance_addons(instance, on_line=lambda line: _broadcast_log_line(ws_group, line))
        full_log = _append_text(output, _append_text(sync_log, refresh_log))
        _broadcast_repo_event(
            instance_id,
            {
                "type": "repo.removed",
                "repo_name": repo_label,
                "status": "removed",
            },
        )
        log_audit(
            instance.created_by,
            AuditLog.Action.OTHER,
            None,
            f"Repo '{repo_label}' removed from instance '{instance.name}'.",
            metadata={"instance_id": instance.id, "repo_name": repo_label},
            organization=instance.organization,
        )
        _job_done(job_id, ok=True, log=full_log)
    except Exception as exc:
        logger.exception("Repo remove failed for repo %s", repo.id)
        _repo_mark_error(repo, str(exc), job_id=job_id)
    finally:
        _release_repo_lock(instance.id, lock_token)


@shared_task(bind=True, max_retries=0)
def swap_instance_repo(self, old_repo_id: int, new_repo_id: int, job_id: int | None = None):
    """Remove old linked repo from the server and clone the new one under a single lock."""
    old_repo = _repo_task_context(old_repo_id)
    new_repo = _repo_task_context(new_repo_id)
    instance = new_repo.instance
    server = instance.server
    _job_start(job_id, self.request.id)

    try:
        lock_token = _acquire_repo_lock(instance.id)
    except RuntimeError as exc:
        _repo_mark_error(new_repo, str(exc), job_id=job_id)
        return

    ws_group = f"odoo.instance.{instance.id}"
    full_log = ""
    try:
        # ── Step 1: remove old repo directory from server ──
        old_label = old_repo.repo_name
        old_path = old_repo.local_path
        _broadcast_log_line(ws_group, f"=== Removing old repo '{old_label}' from server ===")
        if old_path:
            code, rm_out = _ssh_run(
                server,
                f"rm -rf {shlex.quote(old_path)}",
                on_line=lambda line: _broadcast_log_line(ws_group, line),
            )
            if code != 0:
                logger.warning("swap_instance_repo: rm -rf exited %s for %s — continuing anyway", code, old_path)
            full_log = _append_text(full_log, rm_out)

        old_repo.delete()
        _broadcast_repo_event(
            instance.id,
            {"type": "repo.removed", "repo_name": old_label, "status": "removed"},
        )

        # ── Step 2: clone new repo ──
        _broadcast_log_line(ws_group, f"=== Linking new repo '{new_repo.repo_name}' ({new_repo.branch}) ===")
        _update_repo_paths(instance, new_repo)
        instance.addons_sync_status = OdooInstance.AddonsSyncStatus.PENDING
        instance.save(update_fields=["addons_sync_status", "updated_at"])
        _set_repo_status(
            new_repo,
            status=OdooInstanceGitRepo.Status.CLONING,
            last_error="",
            append_log=f"Cloning {new_repo.repo_name} ({new_repo.branch})…",
            reset_log=True,
            started=True,
        )
        new_commit, clone_out = _clean_clone_instance_repo(
            server,
            new_repo,
            new_repo.branch,
            on_line=lambda line: _broadcast_log_line(ws_group, line),
        )
        new_repo.last_pulled_commit = new_commit
        new_repo.previous_commit = ""
        new_repo.last_remote_commit = new_commit
        new_repo.default_branch = new_repo.default_branch or new_repo.branch
        new_repo.last_pulled_at = timezone.now()
        new_repo.last_detected_modules = _detect_repo_modules(server, new_repo.local_path)
        if new_repo.credential_id:
            new_repo.credential.last_used_at = timezone.now()
            new_repo.credential.save(update_fields=["last_used_at", "updated_at"])
        new_repo.save(
            update_fields=[
                "last_pulled_commit",
                "previous_commit",
                "last_remote_commit",
                "default_branch",
                "last_pulled_at",
                "last_detected_modules",
                "updated_at",
            ]
        )
        full_log = _append_text(full_log, clone_out)

        follow_up = _repo_follow_up_success(
            new_repo,
            log_blob=clone_out,
            modules=None,
            allow_refresh_failure=True,
            on_line=lambda line: _broadcast_log_line(ws_group, line),
        )
        full_log = _append_text(full_log, follow_up)
        _job_done(job_id, ok=True, log=full_log)
    except Exception as exc:
        logger.exception("swap_instance_repo failed (old=%s, new=%s)", old_repo_id, new_repo_id)
        try:
            _repo_mark_error(new_repo, str(exc), job_id=job_id)
        except Exception:
            pass
    finally:
        _release_repo_lock(instance.id, lock_token)


@shared_task
def sync_instance_repo_status(repo_id: int):
    repo = _repo_task_context(repo_id)
    server = repo.instance.server
    if not repo.local_path:
        return

    # Check if the .git directory exists before running any git commands.
    # If the directory is missing it means the repo has never been cloned (or
    # was deleted).  Set DISCONNECTED (not ERROR) so the frontend proceeds to
    # call /sync/ which will trigger a fresh clone via update_instance_repo.
    dir_check_cmd = f"test -d {shlex.quote(repo.local_path)}/.git && echo OK || echo MISSING"
    chk_code, chk_out = _ssh_run(server, dir_check_cmd, timeout=15)
    if chk_code != 0:
        # SSH itself failed to connect
        repo.status = OdooInstanceGitRepo.Status.ERROR
        repo.last_error = chk_out or "SSH connection failed."
        repo.save(update_fields=["last_error", "status", "updated_at"])
        return
    if (chk_out or "").strip() == "MISSING":
        # Directory absent — mark DISCONNECTED so /sync/ will re-clone
        repo.status = OdooInstanceGitRepo.Status.DISCONNECTED
        repo.last_error = "Repository directory not found on server. Click Update to re-clone."
        repo.save(update_fields=["last_error", "status", "updated_at"])
        return

    git_setup = ""
    remote_key_path = ""
    if repo.auth_type == OdooInstanceGitRepo.AuthType.SSH_KEY:
        remote_key_path, git_setup = _prepare_remote_git_key(server, repo)
    command = (
        f"cd {shlex.quote(repo.local_path)}"
        f" && {git_setup + ' && ' if git_setup else ''}"
        f"git fetch origin {shlex.quote(repo.branch)}"
        f" && printf 'LOCAL=%s\n' \"$(git rev-parse HEAD)\""
        f" && printf 'REMOTE=%s\n' \"$(git rev-parse FETCH_HEAD)\""
    )
    if remote_key_path:
        command = f"{command} ; rm -f {shlex.quote(remote_key_path)}"
    code, output = _ssh_run(server, command)
    if code != 0:
        repo.status = OdooInstanceGitRepo.Status.ERROR
        repo.last_error = output or "Status sync failed."
    else:
        local_match = re.search(r"LOCAL=([0-9a-fA-F]{7,64})", output)
        remote_match = re.search(r"REMOTE=([0-9a-fA-F]{7,64})", output)
        repo.last_remote_commit = remote_match.group(1) if remote_match else ""
        repo.last_error = ""
        in_sync = local_match and remote_match and local_match.group(1) == remote_match.group(1)
        repo.status = OdooInstanceGitRepo.Status.CONNECTED if in_sync else OdooInstanceGitRepo.Status.DISCONNECTED
    repo.save(update_fields=["last_remote_commit", "last_error", "status", "updated_at"])
    if code == 0 and repo.status == OdooInstanceGitRepo.Status.DISCONNECTED:
        _queue_auto_update_for_repo(repo)


@shared_task
def auto_sync_instance_repos():
    repos = (
        OdooInstanceGitRepo.objects.select_related(
            "instance",
            "instance__server",
        )
        .filter(
            auto_update=True,
            is_enabled=True,
            instance__status=OdooInstance.Status.RUNNING,
        )
        .exclude(status=OdooInstanceGitRepo.Status.CLONING)
    )

    for repo in repos:
        if repo.pinned_commit:
            continue
        try:
            update_instance_repo.delay(repo.id, None)
        except Exception:
            update_instance_repo(repo.id, None)


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
    _record_instance_step(instance, "Starting Docker instance creation")

    if instance.domain:
        try:
            _record_instance_step(instance, "Prewarming domain overlay", f"domain={instance.domain}")
            prewarm_ok, prewarm_message = _reconcile_instance_domain(instance, skip_probe=True)
            if prewarm_message:
                if prewarm_ok:
                    _record_instance_step(instance, "Domain overlay queued", prewarm_message)
                else:
                    _record_instance_error(instance, "Prewarm domain overlay failed", prewarm_message)
        except Exception as exc:
            logger.warning("Docker instance %s: prewarm domain overlay failed", instance.id, exc_info=True)
            _record_instance_error(instance, "Prewarm domain overlay failed", str(exc))

    _pb = os.getenv("ANSIBLE_DOCKER_INSTANCE_PLAYBOOK", "").strip()
    playbook = _pb if (_pb and Path(_pb).exists()) else _default_docker_instance_playbook()
    if not Path(playbook).exists():
        logger.error("Instance %s: Docker instance playbook not found: %s", instance.id, playbook)
        instance.status = OdooInstance.Status.FAILED
        msg = f"Docker instance playbook not found: {playbook}"
        _record_instance_error(instance, "Docker playbook lookup failed", msg)
        instance.save(update_fields=["status", "provisioning_log", "updated_at"])
        _job_done(job_id, ok=False, log=msg)
        return

    client_name = instance.db_name.replace("_", "-")
    container_name = f"odoo-{client_name}"
    cpu_limit = float(instance.requested_cpu_cores or 1)
    mem_limit_mb = int(instance.requested_ram_mb or 1024)
    extra_vars = {
        "client_name": client_name,
        "domain": instance.domain or "",
        "http_port": instance.http_port,
        "db_name": instance.db_name,
        "odoo_version": server.odoo_version,
        "postgres_password": server.docker_postgres_password,
        "restart_policy": instance.restart_policy or "unless-stopped",
        "container_name": container_name,
        "cpu_limit": cpu_limit,
        "mem_limit_mb": mem_limit_mb,
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
    _record_instance_step(
        instance,
        "Running Docker playbook",
        f"playbook={playbook_name}\nserver_ip={server.ip_address}\ndomain={instance.domain or '-'}\ndb_name={instance.db_name}",
    )
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
    domain_message = ""
    if ok:
        _record_instance_step(instance, "Docker playbook finished", "Container setup completed; applying domain configuration.")
        instance.container_name = container_name
        instance.status = OdooInstance.Status.RUNNING
        if instance.domain:
            _record_instance_step(instance, "Reconciling domain overlay", f"domain={instance.domain}")
            domain_ok, domain_message = _reconcile_instance_domain(instance)
            if not domain_ok:
                logger.warning("Docker instance %s domain setup failed: %s", instance.id, domain_message)
                _record_instance_error(instance, "Domain reconciliation failed", domain_message)
            elif domain_message:
                _record_instance_step(instance, "Domain reconciliation finished", domain_message)
        else:
            _save_instance_domain_state(
                instance,
                domain_status=OdooInstance.DomainStatus.NOT_CONFIGURED,
                ssl_status=OdooInstance.SSLStatus.NOT_CONFIGURED,
                ssl_enabled=False,
                ssl_error="",
                checked_at=timezone.now(),
            )
        summary, summary_text = _store_instance_installation_summary(
            instance,
            server=server,
            playbook=playbook,
            ssh_user=ssh_user or "root",
            use_direct=False,
        )
        # Store the admin user password extracted from playbook output
        admin_user_pw = _extract_odoo_admin_user_password(log_blob)
        if admin_user_pw:
            instance.odoo_admin_password = admin_user_pw
            instance.save(update_fields=["odoo_admin_password", "updated_at"])
        _initialize_instance_addons_metadata(instance)
        _record_instance_step(instance, "Addon metadata initialized")
    else:
        instance.status = OdooInstance.Status.FAILED
        _record_instance_error(instance, "Docker playbook failed", "Check the [docker create] section below for the full Ansible output.")
        reachable, message = _probe_server_ssh(server)
        if not reachable:
            _persist_server_reachability(server, reachable=False, message=message)
            _record_instance_error(instance, "Server reachability check failed", message)
    instance.provisioning_log = _append_text(
        instance.provisioning_log,
        "Docker instance created successfully — ready." if ok else "Docker instance creation failed.",
    )
    if domain_message:
        instance.provisioning_log = _append_text(instance.provisioning_log, domain_message)
    instance.save(
        update_fields=[
            "container_name",
            "status",
            "ssl_enabled",
            "domain_status",
            "domain_last_checked_at",
            "ssl_status",
            "ssl_error",
            "provisioning_log",
            "addons_root_path",
            "addons_path_cache",
            "addons_sync_status",
            "addons_last_sync_at",
            "updated_at",
        ]
    )

    access_url = instance.access_url or (f"https://{instance.domain}" if instance.domain else "")
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
            ssl_enabled=instance.ssl_enabled,
            status=instance.status,
            note="Docker container created successfully.",
            deployed_by=instance.created_by,
        )


def _run_docker_instance_delete(instance: OdooInstance, server: OdooServer):
    """
    Internal: run the Docker Odoo instance deletion playbook and mark the instance DELETED.
    Called from delete_odoo_instance when server.deployment_mode == DOCKER.
    """
    _pb = os.getenv("ANSIBLE_DOCKER_INSTANCE_DELETE_PLAYBOOK", "").strip()
    playbook = _pb if (_pb and Path(_pb).exists()) else _default_docker_instance_delete_playbook()
    if not Path(playbook).exists():
        logger.warning("Docker instance delete playbook not found at %s; marking DELETED without cleanup.", playbook)
        instance.status = OdooInstance.Status.DELETED
        instance.provisioning_log = _append_text(
            instance.provisioning_log, f"Delete playbook not found: {playbook}. Marked DELETED without container cleanup."
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


def _run_bare_metal_instance_delete(instance: OdooInstance, server: OdooServer):
    """
    Stop the systemd service, drop the PostgreSQL database, remove the
    instance directory and UFW rule on a bare-metal server, then mark
    the instance DELETED.  Uses the repo-local playbook as the default;
    the env var ANSIBLE_ODOO_INSTANCE_DELETE_PLAYBOOK overrides it.
    """
    _pb = os.getenv("ANSIBLE_ODOO_INSTANCE_DELETE_PLAYBOOK", "").strip()
    playbook = _pb if (_pb and Path(_pb).exists()) else _default_odoo_instance_delete_playbook()
    if not Path(playbook).exists():
        logger.warning(
            "Bare-metal instance delete playbook not found at %s; marking DELETED without cleanup.",
            playbook,
        )
        instance.status = OdooInstance.Status.DELETED
        instance.provisioning_log = _append_text(
            instance.provisioning_log,
            f"Delete playbook not found: {playbook}. Marked DELETED without remote cleanup.",
        )
        instance.save(update_fields=["status", "provisioning_log", "updated_at"])
        return

    extra_vars = {
        "db_name":   instance.db_name,
        "http_port": instance.http_port,
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

    instance.provisioning_log = _append_text(instance.provisioning_log, f"[ansible delete]\n{log_blob}")
    instance.status = OdooInstance.Status.DELETED
    instance.save(update_fields=["status", "provisioning_log", "updated_at"])


@shared_task(bind=True, max_retries=0, time_limit=1800, soft_time_limit=1500)
def configure_docker_host(self, server_id: int, job_id: int | None = None):
    """
    Install Docker on the host, create odoo-network, and start Traefik + PostgreSQL.
    Runs the setup_docker_host.yml Ansible playbook.
    """
    try:
        return _configure_docker_host_inner(self, server_id, job_id)
    except SoftTimeLimitExceeded:
        _mark_server_timed_out(server_id, "configure_docker_host")


def _configure_docker_host_inner(self, server_id: int, job_id: int | None = None):
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

    _pb = os.getenv("ANSIBLE_DOCKER_HOST_PLAYBOOK", "").strip()
    playbook = _pb if (_pb and Path(_pb).exists()) else _default_docker_host_playbook()
    if not Path(playbook).exists():
        server.status = OdooServer.Status.FAILED
        msg = f"Docker host setup playbook not found: {playbook}"
        server.provisioning_log = _append_text(server.provisioning_log, msg)
        server.save(update_fields=["status", "provisioning_log", "updated_at"])
        _job_done(job_id, ok=False, log=msg)
        return

    # Wait for SSH to be ready before running Ansible.
    ws_group = f"odoo.server.{server.id}"
    ip_str = str(server.ip_address)
    _broadcast_server(server.id, "Waiting for SSH to become available…", server.status)
    ssh_ready = False
    for attempt in range(60):  # up to 5 minutes (60 × 5 s)
        try:
            with socket.create_connection((ip_str, 22), timeout=5):
                ssh_ready = True
                break
        except OSError:
            if attempt % 6 == 0:
                logger.info("Server %s: SSH not ready yet (attempt %s/60)…", server.id, attempt + 1)
            time.sleep(5)

    if not ssh_ready:
        logger.error("Server %s: SSH did not become available after 5 minutes.", server.id)
        server.status = OdooServer.Status.FAILED
        msg = "SSH port 22 did not become available within 5 minutes after server was running."
        server.provisioning_log = _append_text(server.provisioning_log, msg)
        server.save(update_fields=["status", "provisioning_log", "updated_at"])
        _broadcast_server(server.id, msg, server.status, done=True)
        _job_done(job_id, ok=False, log=msg)
        return

    logger.info("Server %s: SSH is ready — starting Docker host setup.", server.id)

    acme_email = os.getenv("ODOO_ADMIN_EMAIL", "odoo@example.com").strip()
    pg_password = server.docker_postgres_password or os.getenv("DOCKER_POSTGRES_PASSWORD", "odoo_secret")
    if not server.docker_postgres_password:
        server.docker_postgres_password = pg_password
        server.save(update_fields=["docker_postgres_password"])

    extra_vars = {
        "acme_email": acme_email,
        "postgres_password": pg_password,
        "cf_dns_api_token": os.getenv("PLATFORM_DNS_API_TOKEN", "").strip(),
        "traefik_base_domain": os.getenv("PLATFORM_BASE_DOMAIN", "").strip(),
    }

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
    if ok:
        # Ansible ran successfully over SSH — mark server as reachable so the
        # connectivity badge shows "Connected" and instance creation is unblocked.
        server.is_reachable = True
        server.last_checked_at = timezone.now()
    server.save(update_fields=["status", "is_reachable", "last_checked_at", "provisioning_log", "updated_at"])
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


# ---------------------------------------------------------------------------
# Instance maintenance helpers and tasks
# ---------------------------------------------------------------------------

def _instance_shell_commands(instance: OdooInstance) -> dict:
    """
    Generate instance-specific shell commands for the UI Commands modal.
    Returns a dict with keys: ssh, odoo_shell, update_all_modules,
    restart_service, tail_logs, pip_install.
    """
    runtime = _instance_runtime_context(instance)
    server = instance.server
    db = shlex.quote(instance.db_name)
    service_user = runtime.get("service_user") or "odoo"

    # SSH access command
    host, port = _odoo_server_ssh_target(server)
    if host:
        infra = getattr(server, "infrastructure", None)
        if infra and infra.infra_type == Infrastructure.InfraType.PYOS and infra.external_server:
            ext = infra.external_server
            ssh_user = ext.username or "root"
        else:
            ssh_user = os.getenv("ANSIBLE_SSH_USER", "root").strip() or "root"
        port_flag = f" -p {port}" if port and port != 22 else ""
        ssh_cmd = f"ssh {ssh_user}@{host}{port_flag}"
    else:
        ssh_cmd = ""

    if runtime["mode"] == "docker":
        container = shlex.quote(runtime.get("container_name", ""))
        odoo_shell_cmd = f"docker exec -it {container} odoo shell -c /etc/odoo/odoo.conf -d {db} --no-http"
        update_cmd = (
            f"docker exec {container} odoo -c /etc/odoo/odoo.conf -d {db} "
            f"-u all --stop-after-init --no-http"
        )
        restart_cmd = runtime["restart_command"]
        tail_cmd = f"docker logs -f {container}"
        pip_cmd = ""
    else:
        odoo_bin = runtime.get("odoo_bin", "odoo-bin")
        config = shlex.quote(runtime.get("config_file", ""))

        shell_inner = f"{odoo_bin} shell -c {config} -d {db} --no-http"
        odoo_shell_cmd = f"su -s /bin/bash {shlex.quote(service_user)} -c {shlex.quote(shell_inner)}"

        update_inner = f"{odoo_bin} -c {config} -d {db} -u all --stop-after-init --no-http"
        update_cmd = f"su -s /bin/bash {shlex.quote(service_user)} -c {shlex.quote(update_inner)}"

        restart_cmd = runtime["restart_command"]
        service = shlex.quote(instance.systemd_service or f"odoo-{instance.db_name}")
        tail_cmd = f"journalctl -u {service} -f --output cat"

        summary = instance.installation_summary or {}
        if runtime["mode"] == "bare_direct":
            venv = summary.get("venv_dir") or f"/odoo/instances/{instance.db_name}/venv"
        else:
            odoo_version = server.odoo_version or ""
            odoo_home = f"/opt/odoo{odoo_version}"
            venv = summary.get("venv_dir") or f"{odoo_home}/venv"
        pip_cmd = f"{venv}/bin/pip install <package_name>"

    return {
        "ssh": ssh_cmd,
        "odoo_shell": odoo_shell_cmd,
        "update_all_modules": update_cmd,
        "restart_service": restart_cmd,
        "tail_logs": tail_cmd,
        "pip_install": pip_cmd,
    }


@shared_task(bind=True, max_retries=0)
def update_instance_modules_all(self, instance_id: int, job_id: int | None = None):
    """
    Update all Odoo modules for a bare-metal or Docker instance.
    Runs odoo-bin -u all --stop-after-init --no-http, restarts the service,
    then performs a health check. Progress is streamed over WebSocket.
    """
    import urllib.request

    instance = OdooInstance.objects.select_related(
        "organization",
        "server",
        "server__infrastructure",
        "server__infrastructure__external_server",
    ).get(pk=instance_id)
    server = instance.server

    _job_start(job_id, self.request.id)
    ws_group = f"odoo.instance.{instance_id}"
    log_parts: list[str] = []

    def on_line(line: str):
        log_parts.append(line)
        _broadcast_log_line(ws_group, line)

    try:
        if not server.ip_address:
            raise RuntimeError("Server has no IP address; cannot run module update.")

        runtime = _instance_runtime_context(instance)
        db = shlex.quote(instance.db_name)
        service_user = runtime.get("service_user") or "odoo"

        _broadcast_instance(instance_id, "update_modules", "RUNNING", log="Starting module update…")
        on_line("=== Update All Modules ===")

        if runtime["mode"] == "docker":
            container = shlex.quote(runtime.get("container_name", ""))
            update_cmd = (
                f"docker exec {container} odoo -c /etc/odoo/odoo.conf -d {db} "
                f"-u all --stop-after-init --no-http"
            )
        else:
            odoo_bin = runtime.get("odoo_bin", "odoo-bin")
            config = shlex.quote(runtime.get("config_file", ""))
            inner = f"{odoo_bin} -c {config} -d {db} -u all --stop-after-init --no-http"
            update_cmd = f"su -s /bin/bash {shlex.quote(service_user)} -c {shlex.quote(inner)}"

        on_line(f"Command: {update_cmd}")
        code, output = _ssh_run(server, update_cmd, on_line=on_line, timeout=3600)
        log_parts.append(output)
        if code != 0:
            raise RuntimeError(output or "Module update command returned non-zero exit code.")

        on_line("=== Restarting Service ===")
        restart_cmd = runtime["restart_command"]
        r_code, r_output = _ssh_run(server, restart_cmd, on_line=on_line, timeout=60)
        log_parts.append(r_output)
        if r_code != 0:
            on_line(f"Warning: service restart returned exit code {r_code}; continuing…")

        on_line("=== Health Check ===")
        time.sleep(5)
        health_url = f"http://{server.ip_address}:{instance.http_port}/web/health"
        try:
            with urllib.request.urlopen(health_url, timeout=30) as resp:
                healthy = resp.status == 200
        except Exception:
            healthy = False
        instance.is_reachable = healthy
        instance.last_health_check = timezone.now()
        instance.save(update_fields=["is_reachable", "last_health_check", "updated_at"])
        on_line("Health check passed." if healthy else "Health check failed — instance may still be starting.")

        full_log = "\n".join(log_parts)
        _job_done(job_id, ok=True, log=full_log)
        _broadcast_instance(
            instance_id, "update_modules", "DONE", done=True,
            log="All modules updated successfully." + (" Health OK." if healthy else ""),
        )

    except Exception as exc:
        full_log = "\n".join(log_parts) + f"\nError: {exc}\n{traceback.format_exc()}"
        _job_done(job_id, ok=False, log=full_log)
        _broadcast_instance(instance_id, "update_modules", "FAILED", done=True, log=str(exc))
        logger.exception("update_instance_modules_all failed for instance %s", instance_id)


@shared_task(bind=True, max_retries=0)
def restart_odoo_instance(self, instance_id: int, job_id: int | None = None):
    """
    Restart the Odoo service for an instance, then run a health check.
    Progress is streamed over WebSocket.
    """
    import urllib.request

    instance = OdooInstance.objects.select_related(
        "organization",
        "server",
        "server__infrastructure",
        "server__infrastructure__external_server",
    ).get(pk=instance_id)
    server = instance.server

    _job_start(job_id, self.request.id)
    ws_group = f"odoo.instance.{instance_id}"
    log_parts: list[str] = []

    def on_line(line: str):
        log_parts.append(line)
        _broadcast_log_line(ws_group, line)

    try:
        if not server.ip_address:
            raise RuntimeError("Server has no IP address; cannot restart service.")

        runtime = _instance_runtime_context(instance)
        restart_cmd = runtime["restart_command"]

        _broadcast_instance(instance_id, "restart", "RUNNING", log="Restarting instance…")
        on_line(f"=== Restart Service ===")
        on_line(f"Command: {restart_cmd}")
        code, output = _ssh_run(server, restart_cmd, on_line=on_line, timeout=60)
        log_parts.append(output)
        if code != 0:
            raise RuntimeError(output or "Service restart returned non-zero exit code.")

        on_line("=== Health Check ===")
        time.sleep(5)
        if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
            # Docker containers are not port-exposed to the host; check via domain or docker inspect.
            if instance.domain:
                health_url = f"https://{instance.domain}/web/health"
                try:
                    with urllib.request.urlopen(health_url, timeout=30) as resp:
                        healthy = resp.status == 200
                except Exception:
                    healthy = False
            else:
                container_name = runtime.get("container_name") or f"odoo-{instance.db_name.replace('_', '-')}"
                hc_cmd = f"docker inspect --format='{{{{.State.Health.Status}}}}' {shlex.quote(container_name)}"
                hc_code, hc_out = _ssh_run(server, hc_cmd, timeout=15)
                healthy = hc_code == 0 and hc_out.strip() == "healthy"
        else:
            health_url = f"http://{server.ip_address}:{instance.http_port}/web/health"
            try:
                with urllib.request.urlopen(health_url, timeout=30) as resp:
                    healthy = resp.status == 200
            except Exception:
                healthy = False
        instance.is_reachable = healthy
        instance.last_health_check = timezone.now()
        instance.save(update_fields=["is_reachable", "last_health_check", "updated_at"])
        on_line("Health check passed." if healthy else "Health check failed — instance may still be starting.")

        full_log = "\n".join(log_parts)
        _job_done(job_id, ok=True, log=full_log)
        _broadcast_instance(
            instance_id, "restart", "DONE", done=True,
            log="Instance restarted." + (" Health OK." if healthy else " (Health check failed.)"),
        )

    except Exception as exc:
        full_log = "\n".join(log_parts) + f"\nError: {exc}\n{traceback.format_exc()}"
        _job_done(job_id, ok=False, log=full_log)
        _broadcast_instance(instance_id, "restart", "FAILED", done=True, log=str(exc))
        logger.exception("restart_odoo_instance failed for instance %s", instance_id)


@shared_task(bind=True, max_retries=0)
def stop_odoo_instance(self, instance_id: int, job_id: int | None = None):
    """Stop the Odoo service/container for an instance without deleting it."""
    instance = OdooInstance.objects.select_related(
        "organization",
        "server",
        "server__infrastructure",
        "server__infrastructure__external_server",
    ).get(pk=instance_id)
    server = instance.server

    _job_start(job_id, self.request.id)
    ws_group = f"odoo.instance.{instance_id}"
    log_parts: list[str] = []

    def on_line(line: str):
        log_parts.append(line)
        _broadcast_log_line(ws_group, line)

    try:
        if not server.ip_address:
            raise RuntimeError("Server has no IP address; cannot stop service.")

        runtime = _instance_runtime_context(instance)
        stop_cmd = runtime.get("stop_command")
        if not stop_cmd:
            raise RuntimeError("Stop command not available for this instance mode.")

        _broadcast_instance(instance_id, "stop", instance.status, log="Stopping instance…")
        on_line("=== Stop Service ===")
        on_line(f"Command: {stop_cmd}")
        code, output = _ssh_run(server, stop_cmd, on_line=on_line, timeout=60)
        log_parts.append(output)
        if code != 0:
            raise RuntimeError(output or "Service stop returned non-zero exit code.")

        instance.status = OdooInstance.Status.STOPPED
        instance.is_reachable = False
        instance.last_health_check = timezone.now()
        instance.provisioning_log = _append_text(instance.provisioning_log, "[stop]\nInstance stopped.")
        instance.save(update_fields=["status", "is_reachable", "last_health_check", "provisioning_log", "updated_at"])

        full_log = "\n".join(log_parts)
        _job_done(job_id, ok=True, log=full_log)
        _broadcast_instance(instance_id, "stop", "STOPPED", done=True, log="Instance stopped.")

    except Exception as exc:
        full_log = "\n".join(log_parts) + f"\nError: {exc}\n{traceback.format_exc()}"
        _job_done(job_id, ok=False, log=full_log)
        _broadcast_instance(instance_id, "stop", "FAILED", done=True, log=str(exc))
        logger.exception("stop_odoo_instance failed for instance %s", instance_id)


def _staging_next_available_port(server: OdooServer) -> int | None:
    """Port allocation for staging (duplicates views._next_available_port to avoid circular import)."""
    used = set(
        server.instances.exclude(status=OdooInstance.Status.DELETED).values_list("http_port", flat=True)
    )
    # Also check ports already listening on the server via SSH
    if server.ip_address:
        cmd = (
            f"for port in $(seq {int(server.min_port)} {int(server.max_port)}); do "
            f"ss -ltn \"( sport = :$port )\" 2>/dev/null | tail -n +2 | grep -q . && echo \"$port\"; "
            "done"
        )
        try:
            code, output = _ssh_run(server, cmd, timeout=60)
            if code == 0:
                for line in output.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        used.add(int(line))
        except Exception:
            pass
    for port in range(server.min_port, server.max_port + 1):
        if port not in used:
            return port
    return None


@shared_task(bind=True, max_retries=0)
def create_staging_instance(
    self,
    source_instance_id: int,
    repo_id: int,
    branch: str,
    ttl_days: int = 7,
    auto_delete: bool = True,
    job_id: int | None = None,
):
    """
    Create a staging Odoo Docker instance from a source instance and a git branch.
    The staging instance is a regular OdooInstance tagged via StagingEnvironment.
    """
    source_instance = OdooInstance.objects.select_related(
        "organization",
        "server",
        "server__infrastructure",
        "server__infrastructure__external_server",
    ).get(pk=source_instance_id)
    server = source_instance.server

    try:
        source_repo = OdooInstanceGitRepo.objects.get(pk=repo_id)
    except OdooInstanceGitRepo.DoesNotExist:
        _job_done(job_id, ok=False, log=f"Git repo {repo_id} not found.")
        return

    _job_start(job_id, self.request.id)
    ws_group = f"odoo.instance.{source_instance_id}"

    try:
        # Validate prerequisites
        if source_instance.status != OdooInstance.Status.RUNNING:
            raise RuntimeError(f"Source instance is not RUNNING (status={source_instance.status}).")
        if server.deployment_mode != OdooServer.DeploymentMode.DOCKER:
            raise RuntimeError("Staging environments are only supported on Docker servers.")
        if not server.ip_address:
            raise RuntimeError("Server has no IP address; cannot create staging instance.")

        ttl_days = max(1, min(int(ttl_days or 7), 30))

        # Build naming
        branch_slug = slugify_branch(branch)
        src_slug = slugify_branch(source_instance.db_name)[:20]
        db_name = f"stg_{source_instance.db_name[:20]}_{branch_slug[:20]}"
        db_name = db_name[:63].rstrip("-_")

        # Idempotency: if a live instance with this db_name already exists, abort
        existing = OdooInstance.objects.filter(server=server, db_name=db_name).exclude(
            status=OdooInstance.Status.DELETED
        ).first()
        if existing:
            _job_done(job_id, ok=False, log=f"Staging instance for branch '{branch}' already exists (id={existing.id}).")
            return

        # Port allocation
        port = _staging_next_available_port(server)
        if port is None:
            raise RuntimeError("No available ports on server for staging instance.")

        # Domain label
        label_candidate = normalize_platform_domain_label(f"{branch_slug}-{src_slug}")
        if is_platform_domain_label_valid(label_candidate) and not OdooInstance.objects.filter(
            domain=platform_domain_for_label(label_candidate)
        ).exclude(status=OdooInstance.Status.DELETED).exists():
            label = label_candidate
        else:
            label = build_platform_domain_label()
        platform_domain = platform_domain_for_label(label) if platform_dns_is_configured() else ""

        _broadcast_log_line(ws_group, f"=== Creating staging instance for branch '{branch}' ===")
        _broadcast_log_line(ws_group, f"db_name={db_name}  port={port}  domain={platform_domain or '(none)'}")

        from backups.models import OdooInstanceBackup
        from backups.tasks import backup_odoo_instance, restore_backup_to_new_instance

        source_backup = (
            OdooInstanceBackup.objects.filter(
                instance=source_instance,
                status=OdooInstanceBackup.Status.DONE,
            )
            .order_by("-created_at")
            .first()
        )
        if not source_backup:
            _broadcast_log_line(ws_group, "=== No completed production backup found; creating one now ===")
            backup_odoo_instance(source_instance.id, None)
            source_backup = (
                OdooInstanceBackup.objects.filter(
                    instance=source_instance,
                    status=OdooInstanceBackup.Status.DONE,
                )
                .order_by("-created_at")
                .first()
            )
        if not source_backup:
            raise RuntimeError("Could not create a production backup for staging.")

        _broadcast_log_line(ws_group, f"=== Using production backup #{source_backup.id} to seed staging ===")

        # Create OdooInstance
        staging_inst = OdooInstance.objects.create(
            organization=source_instance.organization,
            server=server,
            name=f"[Staging] {source_instance.name} · {branch_slug}",
            db_name=db_name,
            domain=platform_domain,
            http_port=port,
            domain_status=(
                OdooInstance.DomainStatus.PENDING if platform_domain
                else OdooInstance.DomainStatus.NOT_CONFIGURED
            ),
            ssl_status=(
                OdooInstance.SSLStatus.PENDING if platform_domain and server.tls_mode != OdooServer.TLSMode.DISABLED
                else OdooInstance.SSLStatus.NOT_CONFIGURED
            ),
            requested_cpu_cores=source_instance.requested_cpu_cores,
            requested_ram_mb=source_instance.requested_ram_mb,
            restart_policy="on-failure",
            created_by=source_instance.created_by,
        )

        # Create StagingEnvironment metadata
        staging_env = StagingEnvironment.objects.create(
            staging_instance=staging_inst,
            source_instance=source_instance,
            source_repo=source_repo,
            branch=branch,
            auto_delete_enabled=auto_delete,
            ttl_days=ttl_days,
            created_by=source_instance.created_by,
        )

        # Domain assignment
        if platform_domain:
            _ensure_domain_assignment(staging_inst, platform_domain)

        # Provision + restore production data into the staging instance
        _broadcast_log_line(ws_group, "=== Restoring production copy into staging ===")
        restore_backup_to_new_instance(staging_inst.id, source_backup.id, None)

        # If provisioning succeeded, clone the source repo onto the staging instance
        staging_inst.refresh_from_db()
        if staging_inst.status == OdooInstance.Status.RUNNING:
            _broadcast_log_line(ws_group, f"=== Applying staging code from '{source_repo.repo_name}:{branch}' ===")
            staging_repo = OdooInstanceGitRepo.objects.create(
                instance=staging_inst,
                credential=source_repo.credential,
                repo_name=source_repo.repo_name,
                git_url=source_repo.git_url,
                branch=branch,
                auth_type=source_repo.auth_type,
                auto_update=True,
                install_requirements_on_update=source_repo.install_requirements_on_update,
                auto_upgrade_modules_on_update=source_repo.auto_upgrade_modules_on_update,
                is_enabled=True,
                created_by=source_instance.created_by,
            )
            _dispatch(clone_instance_repo, staging_repo.id)
            staging_env.last_activity_at = timezone.now()
            staging_env.save(update_fields=["last_activity_at", "updated_at"])
        else:
            raise RuntimeError("Staging instance provisioning or restore did not complete successfully.")

        _broadcast_log_line(ws_group, "=== Staging instance creation complete ===")

    except Exception as exc:
        log = f"create_staging_instance failed: {exc}\n{traceback.format_exc()}"
        _job_done(job_id, ok=False, log=log)
        logger.exception("create_staging_instance failed for source_instance=%s branch=%s", source_instance_id, branch)


@shared_task(bind=True, max_retries=0)
def cleanup_expired_staging_instances(self):
    """Periodic beat task: delete staging instances whose TTL has expired."""
    from datetime import timedelta

    now = timezone.now()
    logger.info("cleanup_expired_staging_instances: scanning for expired environments")

    expired = StagingEnvironment.objects.select_related(
        "staging_instance",
        "staging_instance__server",
        "staging_instance__organization",
    ).filter(
        auto_delete_enabled=True,
        staging_instance__status__in=[
            OdooInstance.Status.RUNNING,
            OdooInstance.Status.STOPPED,
            OdooInstance.Status.FAILED,
        ],
    )

    count = 0
    for env in expired:
        if now < env.last_activity_at + timedelta(days=env.ttl_days):
            continue
        staging_inst = env.staging_instance
        logger.info(
            "cleanup_expired_staging: queuing deletion for staging instance %s (branch=%s)",
            staging_inst.id,
            env.branch,
        )
        DeploymentJob.objects.create(
            organization=staging_inst.organization,
            job_type=DeploymentJob.JobType.CLEANUP_STAGING_INSTANCE,
            odoo_instance=staging_inst,
        )
        _dispatch(delete_odoo_instance, staging_inst.id)
        count += 1

    logger.info("cleanup_expired_staging_instances: queued %d deletions", count)


# ---------------------------------------------------------------------------
# Core Odoo auto-update (nightly channel)
# ---------------------------------------------------------------------------

@shared_task(bind=True, name="deployments.tasks.check_and_update_core_odoo", soft_time_limit=3600)
def check_and_update_core_odoo(self, instance_id, job_id=None):
    """Pull the latest Odoo core updates for an instance.

    Docker mode: docker pull <image> + recreate container.
    Bare-metal mode: apt-get upgrade odoo + systemctl restart.
    """
    from deployments.models import DeploymentJob, OdooInstance, OdooServer  # noqa: PLC0415

    instance = OdooInstance.objects.select_related("server").filter(pk=instance_id).first()
    if not instance:
        logger.warning("check_and_update_core_odoo: instance %s not found", instance_id)
        return

    if not instance.auto_update_core:
        logger.info("check_and_update_core_odoo: auto_update_core disabled for instance %s", instance_id)
        return
    if instance.status != OdooInstance.Status.RUNNING:
        logger.info(
            "check_and_update_core_odoo: instance %s not RUNNING (status=%s), skipping",
            instance_id,
            instance.status,
        )
        return

    job = None
    if job_id:
        job = DeploymentJob.objects.filter(pk=job_id).first()

    def _log(msg):
        logger.info("check_and_update_core_odoo[%s]: %s", instance_id, msg)
        if job:
            job.log = (job.log or "") + msg + "\n"
            job.save(update_fields=["log", "updated_at"])

    if job:
        job.status = DeploymentJob.Status.RUNNING
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at", "updated_at"])

    server = instance.server
    is_docker = (getattr(server, "deployment_mode", None) == "DOCKER")

    try:
        if is_docker:
            # Docker: pull latest image tag then recreate container
            odoo_version = getattr(instance, "odoo_version", "17")
            image = f"odoo:{odoo_version}"
            _log(f"Pulling Docker image: {image}")
            rc, out = _ssh_run(server, f"docker pull {image}", timeout=600)
            _log(out)
            if rc != 0:
                raise RuntimeError(f"docker pull failed (rc={rc})")
            container = instance.container_name or f"odoo_{instance.db_name}"
            _log(f"Recreating container: {container}")
            rc, out = _ssh_run(
                server,
                f"docker compose -f /opt/odoo/{instance.db_name}/docker-compose.instance.yml up -d --force-recreate",
                timeout=300,
            )
            _log(out)
            if rc != 0:
                raise RuntimeError(f"docker compose recreate failed (rc={rc})")
        else:
            # Bare-metal: apt-get upgrade + systemctl restart
            _log("Updating Odoo package (nightly channel)…")
            rc, out = _ssh_run(
                server,
                "DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
                "apt-get install -y --only-upgrade odoo 2>&1",
                timeout=600,
            )
            _log(out)
            if rc != 0:
                raise RuntimeError(f"apt-get upgrade odoo failed (rc={rc})")
            service = instance.systemd_service or f"odoo-{instance.db_name}"
            _log(f"Restarting service: {service}")
            rc, out = _ssh_run(server, f"systemctl restart {service}", timeout=120)
            _log(out)
            if rc != 0:
                raise RuntimeError(f"systemctl restart failed (rc={rc})")

        _log("Core Odoo update completed successfully.")
        if job:
            job.status = DeploymentJob.Status.DONE
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "finished_at", "log", "updated_at"])

    except Exception as exc:
        _log(f"ERROR: {exc}")
        if job:
            job.status = DeploymentJob.Status.FAILED
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "finished_at", "log", "updated_at"])
        raise


@shared_task(name="deployments.tasks.auto_check_core_updates")
def auto_check_core_updates():
    """Periodic beat task: trigger core Odoo update for all instances with auto_update_core=True."""
    from deployments.models import DeploymentJob, OdooInstance  # noqa: PLC0415

    instances = OdooInstance.objects.select_related("server", "organization").filter(
        auto_update_core=True,
        status=OdooInstance.Status.RUNNING,
    )
    count = 0
    for instance in instances:
        job = DeploymentJob.objects.create(
            organization=instance.organization,
            job_type=DeploymentJob.JobType.AUTO_UPDATE_CORE,
            odoo_instance=instance,
        )
        check_and_update_core_odoo.delay(instance.id, job.id)
        count += 1
    logger.info("auto_check_core_updates: dispatched %d core update tasks", count)
