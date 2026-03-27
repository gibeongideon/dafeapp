import json
import logging
import os
import re
import shlex
import socket
import subprocess
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

import paramiko
from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from audit.models import AuditLog
from cloud.providers import get_provider
from core.utils import log_audit
from deployments.models import (
    DeploymentJob,
    GitRepositoryCredential,
    Infrastructure,
    Instance,
    OdooInstance,
    OdooInstanceGitRepo,
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
            {"type": "instance.update", "payload": payload},
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


def _instance_runtime_context(instance: OdooInstance) -> dict:
    summary = instance.installation_summary or {}
    server = instance.server
    db_name = instance.db_name
    if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
        client_name = db_name.replace("_", "-")
        addons_root = instance.addons_root_path or f"/data/odoo/{client_name}/addons"
        return {
            "mode": "docker",
            "addons_root_path": addons_root,
            "config_file": summary.get("config_file") or f"/opt/odoo-docker/instances/{client_name}.conf",
            "core_addons_path": "/usr/lib/python3/dist-packages/odoo/addons",
            "container_addons_root": "/var/lib/odoo/addons",
            "restart_command": f"docker restart {shlex.quote(instance.container_name or f'odoo-{client_name}')}",
            "container_name": instance.container_name or f"odoo-{client_name}",
        }

    if summary.get("core_addons_dir"):
        addons_root = instance.addons_root_path or summary.get("custom_addons_dir") or f"/odoo/instances/{db_name}/addons/custom"
        return {
            "mode": "bare_direct",
            "addons_root_path": addons_root,
            "manual_addons_root": summary.get("custom_addons_dir") or addons_root,
            "config_file": summary.get("config_file") or f"/etc/odoo-{db_name}.conf",
            "core_addons_path": summary.get("core_addons_dir") or f"/odoo/instances/{db_name}/addons/core",
            "odoo_bin": f"{summary.get('venv_dir') or f'/odoo/instances/{db_name}/venv'}/bin/python {summary.get('source_dir') or '/odoo/odoo-server'}/odoo-bin",
            "restart_command": f"systemctl restart {shlex.quote(instance.systemd_service or f'odoo-{db_name}')}",
        }

    instance_dir = summary.get("instance_dir") or f"/opt/odoo{server.odoo_version}/instances/{db_name}"
    odoo_home = f"/opt/odoo{server.odoo_version}"
    addons_root = instance.addons_root_path or f"{instance_dir}/addons"
    return {
        "mode": "bare_domain",
        "addons_root_path": addons_root,
        "manual_addons_root": "",
        "config_file": summary.get("config_file") or f"{instance_dir}/odoo.conf",
        "core_addons_path": summary.get("addons_dir") or f"{odoo_home}/src/odoo/addons",
        "odoo_bin": f"{summary.get('venv_dir') or f'{odoo_home}/venv'}/bin/python {summary.get('source_dir') or f'{odoo_home}/src/odoo'}/odoo-bin",
        "restart_command": f"systemctl restart {shlex.quote(instance.systemd_service or f'odoo-{db_name}')}",
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

    if repo_paths:
        parts = [runtime["core_addons_path"], *repo_paths]
    elif runtime["mode"] == "docker":
        parts = [runtime["container_addons_root"], runtime["core_addons_path"]]
    elif runtime.get("manual_addons_root"):
        parts = [runtime["core_addons_path"], runtime["manual_addons_root"]]
    else:
        parts = [runtime["core_addons_path"]]
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
                f"-u {shlex.quote(','.join(modules))} --stop-after-init"
            )
        shell_payload = "env['ir.module.module'].update_list(); env.cr.commit(); print('module list refreshed')"
        return (
            f"printf '%s\n' {shlex.quote(shell_payload)} | "
            f"docker exec -i {container} odoo shell -c /etc/odoo/odoo.conf -d {shlex.quote(instance.db_name)}"
        )

    odoo_bin = runtime["odoo_bin"]
    config_file = runtime["config_file"]
    if modules:
        return (
            f"{odoo_bin} -c {shlex.quote(config_file)} -d {shlex.quote(instance.db_name)} "
            f"-u {shlex.quote(','.join(modules))} --stop-after-init"
        )
    shell_payload = "env['ir.module.module'].update_list(); env.cr.commit(); print('module list refreshed')"
    return (
        f"printf '%s\n' {shlex.quote(shell_payload)} | "
        f"{odoo_bin} shell -c {shlex.quote(config_file)} -d {shlex.quote(instance.db_name)}"
    )


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


def _restart_and_refresh_instance_addons(
    instance: OdooInstance,
    *,
    modules: list[str] | None = None,
    on_line=None,
) -> str:
    server = instance.server
    runtime = _instance_runtime_context(instance)
    commands = [
        runtime["restart_command"],
        _instance_refresh_module_command(instance, modules=None),
    ]
    if modules:
        commands.append(_instance_refresh_module_command(instance, modules=modules))
    code, output = _ssh_run(server, " && ".join(commands), on_line=on_line)
    if code != 0:
        raise RuntimeError(output or "Failed to restart Odoo and refresh its module registry.")
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
        _initialize_instance_addons_metadata(instance)
    else:
        instance.status = OdooInstance.Status.FAILED
    instance.provisioning_log = _append_text(
        instance.provisioning_log,
        "Instance created successfully — ready." if ok else "Instance creation failed.",
    )
    instance.save(
        update_fields=[
            "status",
            "systemd_service",
            "nginx_site",
            "ssl_enabled",
            "provisioning_log",
            "addons_root_path",
            "addons_path_cache",
            "addons_sync_status",
            "addons_last_sync_at",
            "updated_at",
        ]
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
    """Run the deletion playbook, then remove the instance record completely."""
    instance = OdooInstance.objects.select_related(
        "organization",
        "server",
        "server__infrastructure",
        "server__infrastructure__external_server",
    ).get(pk=instance_id)
    server = instance.server

    try:
        if server.ip_address:
            if server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
                _run_docker_instance_delete(instance, server)
            else:
                playbook = os.getenv("ANSIBLE_ODOO_INSTANCE_DELETE_PLAYBOOK", "").strip()
                if playbook:
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
                else:
                    instance.status = OdooInstance.Status.DELETED
                    instance.provisioning_log = _append_text(instance.provisioning_log, "No delete playbook configured; removing record.")
                    instance.save(update_fields=["status", "provisioning_log", "updated_at"])
        else:
            instance.status = OdooInstance.Status.DELETED
            instance.provisioning_log = _append_text(instance.provisioning_log, "Server IP unavailable; skipped remote cleanup.")
            instance.save(update_fields=["status", "provisioning_log", "updated_at"])
    except Exception:
        logger.warning("Instance %s cleanup failed; removing database record anyway.", instance_id, exc_info=True)
    finally:
        _broadcast_instance_removed(instance.id, server.id)
        instance.delete()
        _broadcast_server_snapshot(server)


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
    on_line=None,
):
    _append_repo_log(repo, log_blob, reset=False)
    sync_log = _sync_instance_addons_config(repo.instance, on_line=on_line)
    refresh_log = _restart_and_refresh_instance_addons(repo.instance, modules=modules, on_line=on_line)
    _append_repo_log(repo, sync_log)
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

        clone_url = _repo_clone_url(repo)
        git_setup = ""
        remote_key_path = ""
        if repo.auth_type == OdooInstanceGitRepo.AuthType.SSH_KEY:
            remote_key_path, git_setup = _prepare_remote_git_key(server, repo)

        clone_cmd = (
            f"mkdir -p {shlex.quote(instance.addons_root_path or _instance_runtime_context(instance)['addons_root_path'])} "
            f"&& rm -rf {shlex.quote(repo.local_path)} "
            f"&& {git_setup + ' && ' if git_setup else ''}"
            f"git clone --branch {shlex.quote(repo.branch)} --single-branch "
            f"{shlex.quote(clone_url)} {shlex.quote(repo.local_path)}"
        )
        if remote_key_path:
            clone_cmd = f"{clone_cmd} ; rm -f {shlex.quote(remote_key_path)}"

        code, log_blob = _ssh_run(server, clone_cmd, on_line=lambda line: _broadcast_log_line(ws_group, line))
        if code != 0:
            raise RuntimeError(log_blob or "Clone failed.")

        head_code, head_output = _ssh_run(server, f"git -C {shlex.quote(repo.local_path)} rev-parse HEAD")
        if head_code != 0:
            raise RuntimeError(head_output or "Could not read the cloned commit SHA.")
        repo.last_pulled_commit = head_output.strip()
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

        full_log = _repo_follow_up_success(repo, log_blob=log_blob, modules=None, on_line=lambda line: _broadcast_log_line(ws_group, line))
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

        git_setup = ""
        remote_key_path = ""
        if repo.auth_type == OdooInstanceGitRepo.AuthType.SSH_KEY:
            remote_key_path, git_setup = _prepare_remote_git_key(server, repo)

        remote_cmd = (
            f"cd {shlex.quote(repo.local_path)}"
            f" && {git_setup + ' && ' if git_setup else ''}"
            f"git fetch origin {shlex.quote(repo.branch)}"
            f" && printf 'LOCAL=%s\n' \"$(git rev-parse HEAD)\""
            f" && printf 'REMOTE=%s\n' \"$(git rev-parse FETCH_HEAD)\""
        )
        if remote_key_path:
            remote_cmd = f"{remote_cmd} ; rm -f {shlex.quote(remote_key_path)}"

        code, log_blob = _ssh_run(server, remote_cmd, on_line=lambda line: _broadcast_log_line(ws_group, line))
        if code != 0:
            raise RuntimeError(log_blob or "Fetch failed.")

        local_match = re.search(r"LOCAL=([0-9a-fA-F]{7,64})", log_blob)
        remote_match = re.search(r"REMOTE=([0-9a-fA-F]{7,64})", log_blob)
        local_commit = local_match.group(1) if local_match else ""
        remote_commit = remote_match.group(1) if remote_match else ""
        repo.last_remote_commit = remote_commit
        if not remote_commit:
            raise RuntimeError("Could not determine the remote commit for this branch.")
        if local_commit == remote_commit:
            repo.status = OdooInstanceGitRepo.Status.CONNECTED
            repo.last_sync_finished_at = timezone.now()
            repo.last_error = ""
            _append_repo_log(repo, _append_text(log_blob, "Already up to date."), reset=True)
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

        pull_cmd = (
            f"cd {shlex.quote(repo.local_path)}"
            f" && git checkout {shlex.quote(repo.branch)}"
            f" && git pull --ff-only origin {shlex.quote(repo.branch)}"
            f" && git rev-parse HEAD"
        )
        code, pull_output = _ssh_run(server, pull_cmd, on_line=lambda line: _broadcast_log_line(ws_group, line))
        if code != 0:
            raise RuntimeError(pull_output or "Pull failed.")

        new_commit = pull_output.splitlines()[-1].strip()
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
            log_blob=_append_text(log_blob, pull_output),
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

        git_setup = ""
        remote_key_path = ""
        if repo.auth_type == OdooInstanceGitRepo.AuthType.SSH_KEY:
            remote_key_path, git_setup = _prepare_remote_git_key(server, repo)

        previous_commit = repo.last_pulled_commit or ""
        branch_cmd = (
            f"cd {shlex.quote(repo.local_path)}"
            f" && {git_setup + ' && ' if git_setup else ''}"
            f"git fetch origin {shlex.quote(branch)}"
            f" && git checkout {shlex.quote(branch)}"
            f" && git pull --ff-only origin {shlex.quote(branch)}"
            f" && git rev-parse HEAD"
        )
        if remote_key_path:
            branch_cmd = f"{branch_cmd} ; rm -f {shlex.quote(remote_key_path)}"

        code, output = _ssh_run(server, branch_cmd, on_line=lambda line: _broadcast_log_line(ws_group, line))
        if code != 0:
            raise RuntimeError(output or "Branch checkout failed.")

        new_commit = output.splitlines()[-1].strip()
        changed_modules = _detect_changed_modules(server, repo.local_path, previous_commit, new_commit)
        repo.branch = branch
        repo.previous_commit = previous_commit
        repo.last_pulled_commit = new_commit
        repo.last_remote_commit = new_commit
        repo.last_pulled_at = timezone.now()
        repo.last_detected_modules = changed_modules or _detect_repo_modules(server, repo.local_path)
        repo.save(
            update_fields=[
                "branch",
                "previous_commit",
                "last_pulled_commit",
                "last_remote_commit",
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


@shared_task
def sync_instance_repo_status(repo_id: int):
    repo = _repo_task_context(repo_id)
    server = repo.instance.server
    if not repo.local_path:
        return
    code, output = _ssh_run(
        server,
        (
            f"cd {shlex.quote(repo.local_path)}"
            f" && git fetch origin {shlex.quote(repo.branch)}"
            f" && printf 'LOCAL=%s\n' \"$(git rev-parse HEAD)\""
            f" && printf 'REMOTE=%s\n' \"$(git rev-parse FETCH_HEAD)\""
        ),
    )
    if code != 0:
        repo.status = OdooInstanceGitRepo.Status.ERROR
        repo.last_error = output or "Status sync failed."
    else:
        local_match = re.search(r"LOCAL=([0-9a-fA-F]{7,64})", output)
        remote_match = re.search(r"REMOTE=([0-9a-fA-F]{7,64})", output)
        repo.last_remote_commit = remote_match.group(1) if remote_match else ""
        repo.last_error = ""
        repo.status = (
            OdooInstanceGitRepo.Status.CONNECTED
            if local_match and remote_match and local_match.group(1) == remote_match.group(1)
            else OdooInstanceGitRepo.Status.DISCONNECTED
        )
    repo.save(update_fields=["last_remote_commit", "last_error", "status", "updated_at"])


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
        _initialize_instance_addons_metadata(instance)
    else:
        instance.status = OdooInstance.Status.FAILED
    instance.provisioning_log = _append_text(
        instance.provisioning_log,
        "Docker instance created successfully — ready." if ok else "Docker instance creation failed.",
    )
    instance.save(
        update_fields=[
            "container_name",
            "status",
            "ssl_enabled",
            "provisioning_log",
            "addons_root_path",
            "addons_path_cache",
            "addons_sync_status",
            "addons_last_sync_at",
            "updated_at",
        ]
    )

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
