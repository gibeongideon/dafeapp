"""
Celery tasks for instance backup and restore.

Both bare-metal (systemd) and Docker deployment modes are supported.

Bare-metal paths are resolved from installation_summary, falling back to
the same conventions used in deployments/tasks.py:
  - data_dir  : summary["data_dir"] or /opt/odoo{version}/instances/{db}/data
  - filestore : {data_dir}/filestore/{db}
  - service   : instance.systemd_service or odoo-{db}

Docker paths follow infra/docker/scripts/backup.sh conventions:
  - filestore  : /data/odoo/{db}
  - pg container: odoo-postgres  user: odoo
  - container  : instance.container_name or odoo-{db}
"""

import logging
import os
import shlex
import tempfile
from datetime import datetime, timezone as dt_timezone

from celery import shared_task
from django.utils import timezone

from backups.models import OdooInstanceBackup
from deployments.models import DeploymentJob, OdooInstance, OdooServer
from deployments.tasks import (
    _connect_ssh_client,
    _default_docker_instance_playbook,
    _default_odoo_instance_direct_playbook,
    _job_done,
    _job_start,
    _run_ansible_playbook,
    _run_docker_instance_create,
    _server_ansible_creds,
    _ssh_run,
    _store_instance_installation_summary,
)

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────────

def _timestamp() -> str:
    return datetime.now(dt_timezone.utc).strftime("%Y%m%d_%H%M%S")


def _bare_metal_data_dir(instance: OdooInstance) -> str:
    summary = instance.installation_summary or {}
    if summary.get("data_dir"):
        return summary["data_dir"]
    version = instance.server.odoo_version
    db = instance.db_name
    # Direct-mode servers store instances under /odoo/instances/
    if summary.get("instance_dir", "").startswith("/odoo/instances/"):
        return f"/odoo/instances/{db}/data"
    return f"/opt/odoo{version}/instances/{db}/data"


def _log_append(existing: str, line: str) -> str:
    return (existing + "\n" + line).strip()


def _dispatch(task, *args):
    try:
        task.delay(*args)
    except Exception:
        logger.warning("Celery broker unavailable; running task synchronously.", exc_info=True)
        task(*args)


@shared_task(bind=True, max_retries=0)
def run_scheduled_instance_backup(self, instance_id: int):
    """
    Beat entrypoint for per-instance scheduled backups.
    Creates a DeploymentJob and then dispatches the normal backup task.
    """
    try:
        instance = OdooInstance.objects.select_related("organization", "server").get(pk=instance_id)
    except OdooInstance.DoesNotExist:
        logger.warning("Scheduled backup skipped; instance %s no longer exists.", instance_id)
        return

    schedule = getattr(instance, "backup_schedule", None)
    if not schedule or not schedule.enabled:
        logger.info("Scheduled backup skipped for instance %s; schedule is disabled.", instance_id)
        return
    if instance.status != OdooInstance.Status.RUNNING:
        logger.info("Scheduled backup skipped for instance %s; status is %s.", instance_id, instance.status)
        return
    if OdooInstanceBackup.objects.filter(instance=instance, status=OdooInstanceBackup.Status.RUNNING).exists():
        logger.info("Scheduled backup skipped for instance %s; another backup is already running.", instance_id)
        return
    if DeploymentJob.objects.filter(
        odoo_instance=instance,
        job_type=DeploymentJob.JobType.BACKUP_INSTANCE,
        status__in=[DeploymentJob.Status.QUEUED, DeploymentJob.Status.RUNNING],
    ).exists():
        logger.info("Scheduled backup skipped for instance %s; a backup job is already queued or running.", instance_id)
        return

    job = DeploymentJob.objects.create(
        organization=instance.organization,
        job_type=DeploymentJob.JobType.BACKUP_INSTANCE,
        odoo_instance=instance,
        odoo_server=instance.server,
        created_by=schedule.created_by,
    )
    _dispatch(backup_odoo_instance, instance.pk, job.pk)


# ── backup ────────────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=0)
def backup_odoo_instance(self, instance_id: int, job_id: int | None = None):
    """
    Create a point-in-time backup of an OdooInstance.

    Writes db.sql.gz and (for FULL backups) filestore.tar.gz to
    /backups/dafeapp/{db_name}/{timestamp}/ on the target server.
    """
    _job_start(job_id, self.request.id)

    try:
        instance = OdooInstance.objects.select_related("server", "organization").get(pk=instance_id)
    except OdooInstance.DoesNotExist:
        _job_done(job_id, ok=False, log=f"OdooInstance {instance_id} not found.")
        return

    server = instance.server
    db_name = instance.db_name
    ts = _timestamp()
    backup_dir = f"/backups/dafeapp/{shlex.quote(db_name)}/{ts}"

    # Resolve created_by from the DeploymentJob
    created_by = None
    if job_id:
        created_by = DeploymentJob.objects.filter(pk=job_id).values_list("created_by", flat=True).first()

    backup = OdooInstanceBackup.objects.create(
        organization=instance.organization,
        instance=instance,
        backup_type=OdooInstanceBackup.BackupType.FULL,
        status=OdooInstanceBackup.Status.RUNNING,
        backup_dir=backup_dir,
        created_by=created_by,
    )

    log = ""

    def step(msg: str):
        nonlocal log
        log = _log_append(log, msg)
        logger.info("[backup #%s] %s", backup.pk, msg)

    try:
        # 1. Create backup directory on remote server
        step(f"Creating backup directory: {backup_dir}")
        code, out = _ssh_run(server, f"mkdir -p {backup_dir}")
        if code != 0:
            raise RuntimeError(f"Failed to create backup dir: {out}")

        db_path = f"{backup_dir}/db.sql.gz"
        fs_path = f"{backup_dir}/filestore.tar.gz"

        if server.deployment_mode == OdooServer.DeploymentMode.BARE_METAL:
            # ── Database ──────────────────────────────────────────────────────
            step(f"Dumping database '{db_name}' (bare-metal pg_dump)…")
            dump_cmd = (
                f"sudo -u postgres pg_dump {shlex.quote(db_name)} "
                f"| gzip > {db_path}"
            )
            code, out = _ssh_run(server, dump_cmd, timeout=3600)
            if code != 0:
                raise RuntimeError(f"pg_dump failed: {out}")
            step("Database dump complete.")

            # ── Filestore ─────────────────────────────────────────────────────
            data_dir = _bare_metal_data_dir(instance)
            filestore_parent = f"{data_dir}/filestore"
            filestore_dir = f"{filestore_parent}/{db_name}"

            step(f"Archiving filestore: {filestore_dir}")
            tar_cmd = (
                f"if [ -d {shlex.quote(filestore_dir)} ]; then "
                f"tar -czf {fs_path} -C {shlex.quote(filestore_parent)} {shlex.quote(db_name)}; "
                f"else echo 'no filestore directory found, skipping'; fi"
            )
            code, out = _ssh_run(server, tar_cmd, timeout=3600)
            if code != 0:
                raise RuntimeError(f"Filestore archive failed: {out}")
            step("Filestore archive complete.")

        else:  # DOCKER
            # ── Database ──────────────────────────────────────────────────────
            step(f"Dumping database '{db_name}' (docker pg_dump)…")
            dump_cmd = (
                f"docker exec odoo-postgres pg_dump -U odoo {shlex.quote(db_name)} "
                f"| gzip > {db_path}"
            )
            code, out = _ssh_run(server, dump_cmd, timeout=3600)
            if code != 0:
                raise RuntimeError(f"pg_dump (docker) failed: {out}")
            step("Database dump complete.")

            # ── Filestore ─────────────────────────────────────────────────────
            filestore_parent = "/data/odoo"
            filestore_dir = f"{filestore_parent}/{db_name}"

            step(f"Archiving filestore: {filestore_dir}")
            tar_cmd = (
                f"if [ -d {shlex.quote(filestore_dir)} ]; then "
                f"tar -czf {fs_path} -C {shlex.quote(filestore_parent)} {shlex.quote(db_name)}; "
                f"else echo 'no filestore directory found, skipping'; fi"
            )
            code, out = _ssh_run(server, tar_cmd, timeout=3600)
            if code != 0:
                raise RuntimeError(f"Filestore archive failed: {out}")
            step("Filestore archive complete.")

        # ── Size ──────────────────────────────────────────────────────────────
        size_bytes = 0
        size_code, size_out = _ssh_run(server, f"du -sb {backup_dir} 2>/dev/null | cut -f1")
        if size_code == 0 and size_out.strip().isdigit():
            size_bytes = int(size_out.strip())

        # Detect whether filestore archive was actually created
        fs_exists_code, _ = _ssh_run(server, f"test -f {fs_path} && echo yes || echo no")
        fs_path_saved = fs_path if (fs_exists_code == 0 and _.strip() == "yes") else ""

        backup.status = OdooInstanceBackup.Status.DONE
        backup.db_backup_path = db_path
        backup.filestore_backup_path = fs_path_saved
        backup.size_bytes = size_bytes
        backup.log = log
        backup.save(update_fields=["status", "db_backup_path", "filestore_backup_path", "size_bytes", "log", "updated_at"])

        step(f"Backup complete. Size: {backup.size_display}")
        _job_done(job_id, ok=True, log=log)

    except Exception as exc:
        logger.exception("backup_odoo_instance failed for instance %s", instance_id)
        error_log = _log_append(log, f"ERROR: {exc}")
        backup.status = OdooInstanceBackup.Status.FAILED
        backup.log = error_log
        backup.save(update_fields=["status", "log", "updated_at"])
        _job_done(job_id, ok=False, log=error_log)


# ── restore ───────────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=0)
def restore_odoo_instance(self, instance_id: int, backup_id: int, job_id: int | None = None):
    """
    Restore an OdooInstance from a previously created OdooInstanceBackup.

    Steps:
      1. Stop the Odoo service / container
      2. Drop and recreate the PostgreSQL database
      3. Restore the database from db.sql.gz
      4. Restore the filestore from filestore.tar.gz (if present)
      5. Fix file ownership
      6. Restart the Odoo service / container
    """
    _job_start(job_id, self.request.id)

    try:
        backup = OdooInstanceBackup.objects.select_related(
            "instance", "instance__server", "instance__organization"
        ).get(pk=backup_id)
    except OdooInstanceBackup.DoesNotExist:
        _job_done(job_id, ok=False, log=f"OdooInstanceBackup {backup_id} not found.")
        return

    instance = backup.instance
    server = instance.server
    db_name = instance.db_name
    log = ""

    def step(msg: str):
        nonlocal log
        log = _log_append(log, msg)
        logger.info("[restore backup #%s → instance #%s] %s", backup.pk, instance.pk, msg)

    def run(cmd: str, *, timeout: int = 1800, allow_failure: bool = False) -> tuple[int, str]:
        code, out = _ssh_run(server, cmd, timeout=timeout)
        if out:
            step(out[:500])
        if code != 0 and not allow_failure:
            raise RuntimeError(f"Command failed (exit {code}): {cmd[:120]}\n{out[:300]}")
        return code, out

    try:
        if server.deployment_mode == OdooServer.DeploymentMode.BARE_METAL:
            svc = shlex.quote(instance.systemd_service or f"odoo-{db_name}")

            # 1. Stop service
            step(f"Stopping service {svc}…")
            run(f"sudo systemctl stop {svc} 2>/dev/null || true", allow_failure=True)

            # 2. Drop + create database
            step(f"Dropping database '{db_name}'…")
            run(f"sudo -u postgres dropdb --if-exists {shlex.quote(db_name)}")
            step(f"Creating database '{db_name}'…")
            run(f"sudo -u postgres createdb -O odoo {shlex.quote(db_name)}")

            # 3. Restore database
            step(f"Restoring database from {backup.db_backup_path}…")
            run(
                f"gunzip -c {backup.db_backup_path} | sudo -u postgres psql {shlex.quote(db_name)}",
                timeout=3600,
            )
            step("Database restored.")

            # 4. Restore filestore
            if backup.filestore_backup_path:
                data_dir = _bare_metal_data_dir(instance)
                filestore_parent = f"{data_dir}/filestore"
                filestore_dir = f"{filestore_parent}/{db_name}"

                step(f"Restoring filestore to {filestore_dir}…")
                run(f"rm -rf {shlex.quote(filestore_dir)}")
                run(f"mkdir -p {shlex.quote(filestore_parent)}")
                run(
                    f"tar -xzf {backup.filestore_backup_path} -C {shlex.quote(filestore_parent)}",
                    timeout=3600,
                )
                run(f"chown -R odoo:odoo {shlex.quote(filestore_dir)}", allow_failure=True)
                step("Filestore restored.")

            # 5. Restart service
            step(f"Starting service {svc}…")
            run(f"sudo systemctl start {svc}")

        else:  # DOCKER
            container = shlex.quote(instance.container_name or f"odoo-{db_name}")

            # 1. Stop container
            step(f"Stopping container {container}…")
            run(f"docker stop {container} 2>/dev/null || true", allow_failure=True)

            # 2. Drop + create database
            step(f"Dropping database '{db_name}'…")
            run(f"docker exec odoo-postgres dropdb -U odoo --if-exists {shlex.quote(db_name)}")
            step(f"Creating database '{db_name}'…")
            run(f"docker exec odoo-postgres createdb -U odoo -d postgres {shlex.quote(db_name)}")

            # 3. Restore database
            step(f"Restoring database from {backup.db_backup_path}…")
            run(
                f"gunzip -c {backup.db_backup_path} "
                f"| docker exec -i odoo-postgres psql -U odoo {shlex.quote(db_name)}",
                timeout=3600,
            )
            step("Database restored.")

            # 4. Restore filestore
            if backup.filestore_backup_path:
                filestore_parent = "/data/odoo"
                filestore_dir = f"{filestore_parent}/{db_name}"

                step(f"Restoring filestore to {filestore_dir}…")
                run(f"rm -rf {shlex.quote(filestore_dir)}")
                run(f"mkdir -p {shlex.quote(filestore_parent)}")
                run(
                    f"tar -xzf {backup.filestore_backup_path} -C {shlex.quote(filestore_parent)}",
                    timeout=3600,
                )
                # uid=100 gid=101 is the odoo user inside the official Docker image
                run(f"chown -R 100:101 {shlex.quote(filestore_dir)}", allow_failure=True)
                step("Filestore restored.")

            # 5. Start container
            step(f"Starting container {container}…")
            run(f"docker start {container}")

        step("Restore complete.")
        _job_done(job_id, ok=True, log=log)

    except Exception as exc:
        logger.exception("restore_odoo_instance failed for backup %s", backup_id)
        error_log = _log_append(log, f"ERROR: {exc}")
        _job_done(job_id, ok=False, log=error_log)


# ── restore to new instance ───────────────────────────────────────────────────

def _sftp_transfer_file(src_server: OdooServer, src_path: str, dst_server: OdooServer, dst_path: str) -> None:
    """
    Transfer a file from src_server to dst_server through DafeApp (download then upload).
    Uses paramiko SFTP on both ends.
    """
    src_client = dst_client = src_sftp = dst_sftp = None
    tmp_path = None
    try:
        # Download from source server to a local temp file
        src_client, src_tmp_key = _connect_ssh_client(src_server)
        src_sftp = src_client.open_sftp()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tf:
            tmp_path = tf.name
        src_sftp.get(src_path, tmp_path)
        src_sftp.close()
        src_client.close()
        if src_tmp_key:
            try:
                os.unlink(src_tmp_key)
            except OSError:
                pass
        src_sftp = src_client = None

        # Upload to destination server
        dst_client, dst_tmp_key = _connect_ssh_client(dst_server)
        dst_sftp = dst_client.open_sftp()
        dst_sftp.put(tmp_path, dst_path)
        dst_sftp.close()
        dst_client.close()
        if dst_tmp_key:
            try:
                os.unlink(dst_tmp_key)
            except OSError:
                pass
    finally:
        if src_sftp:
            try:
                src_sftp.close()
            except Exception:
                pass
        if src_client:
            try:
                src_client.close()
            except Exception:
                pass
        if dst_sftp:
            try:
                dst_sftp.close()
            except Exception:
                pass
        if dst_client:
            try:
                dst_client.close()
            except Exception:
                pass
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@shared_task(bind=True, max_retries=0)
def restore_backup_to_new_instance(
    self,
    new_instance_id: int,
    backup_id: int,
    job_id: int | None = None,
):
    """
    Spin up a new OdooInstance and restore an existing backup into it.

    Flow:
      1. Set up the new Odoo service via Ansible (same as create_odoo_instance)
      2. If the backup lives on a different server, transfer files to the new server
      3. Stop the freshly-created Odoo service/container
      4. Drop the auto-created database; create a clean one
      5. Restore the database dump
      6. Restore the filestore
      7. Fix ownership and restart
    """
    _job_start(job_id, self.request.id)

    try:
        new_instance = OdooInstance.objects.select_related(
            "server", "server__infrastructure",
            "server__infrastructure__external_server",
            "organization",
        ).get(pk=new_instance_id)
    except OdooInstance.DoesNotExist:
        _job_done(job_id, ok=False, log=f"OdooInstance {new_instance_id} not found.")
        return

    try:
        backup = OdooInstanceBackup.objects.select_related(
            "instance__server"
        ).get(pk=backup_id)
    except OdooInstanceBackup.DoesNotExist:
        _job_done(job_id, ok=False, log=f"OdooInstanceBackup {backup_id} not found.")
        return

    target_server = new_instance.server
    source_server = backup.instance.server
    db_name       = new_instance.db_name
    log = ""

    def step(msg: str):
        nonlocal log
        log = _log_append(log, msg)
        logger.info("[restore-to-new #%s → instance #%s] %s", backup.pk, new_instance.pk, msg)

    def run(cmd: str, server: OdooServer = None, *, timeout: int = 1800, allow_failure: bool = False) -> tuple[int, str]:
        s = server or target_server
        code, out = _ssh_run(s, cmd, timeout=timeout)
        if out:
            step(out[:500])
        if code != 0 and not allow_failure:
            raise RuntimeError(f"Command failed (exit {code}): {cmd[:120]}\n{out[:300]}")
        return code, out

    try:
        new_instance.status = OdooInstance.Status.CONFIGURING
        new_instance.save(update_fields=["status", "updated_at"])

        # ── Phase 1: Create the new Odoo service via Ansible ─────────────────
        step(f"Creating new instance '{new_instance.db_name}' on server {target_server.ip_address}…")

        if target_server.deployment_mode == OdooServer.DeploymentMode.DOCKER:
            step("Docker mode: running create_docker_odoo_instance playbook…")
            # _run_docker_instance_create sets instance status internally
            _run_docker_instance_create(new_instance, target_server, job_id, self.request.id)
            # After docker creation, re-fetch instance to get updated status
            new_instance.refresh_from_db()
            if new_instance.status == OdooInstance.Status.FAILED:
                raise RuntimeError("Docker instance creation playbook failed.")
        else:
            playbook = _default_odoo_instance_direct_playbook()
            extra_vars = {
                "odoo_version": target_server.odoo_version,
                "db_name": db_name,
                "instance_name": new_instance.name,
                "http_port": new_instance.http_port,
                "restart_policy": new_instance.restart_policy,
                "proxy_mode": bool(new_instance.domain),
            }
            ssh_user, ssh_key, ssh_password, tmp_key = _server_ansible_creds(target_server)
            try:
                ansible_ok, ansible_log = _run_ansible_playbook(
                    playbook,
                    str(target_server.ip_address),
                    extra_vars,
                    ssh_user=ssh_user,
                    ssh_key_path=ssh_key,
                    ssh_password=ssh_password,
                )
            finally:
                if tmp_key:
                    try:
                        os.unlink(tmp_key)
                    except OSError:
                        pass
            log = _log_append(log, ansible_log)
            if not ansible_ok:
                raise RuntimeError("Instance creation playbook failed. See log for details.")

            new_instance.systemd_service = f"odoo-{db_name}"
            new_instance.status = OdooInstance.Status.RUNNING
            summary, summary_text = _store_instance_installation_summary(
                new_instance,
                server=target_server,
                playbook=playbook,
                ssh_user=ssh_user or "root",
                use_direct=True,
            )
            new_instance.save(update_fields=[
                "status", "systemd_service",
                "installation_summary", "installation_summary_text",
                "addons_root_path", "addons_path_cache",
                "addons_sync_status", "addons_last_sync_at",
                "updated_at",
            ])

        step("New Odoo instance provisioned. Proceeding with backup restore…")

        # ── Phase 2: Transfer backup files if source ≠ target server ─────────
        db_path = backup.db_backup_path
        fs_path = backup.filestore_backup_path

        same_server = (source_server.pk == target_server.pk)
        if not same_server:
            ts = _timestamp()
            transfer_dir = f"/backups/dafeapp/.transfers/{db_name}/{ts}"
            step(f"Source and target servers differ — transferring backup files via DafeApp…")
            run(f"mkdir -p {transfer_dir}")

            # Transfer DB dump
            dst_db_path = f"{transfer_dir}/db.sql.gz"
            step(f"Transferring database dump…")
            _sftp_transfer_file(source_server, db_path, target_server, dst_db_path)
            db_path = dst_db_path
            step("Database dump transferred.")

            # Transfer filestore archive if present
            if fs_path:
                dst_fs_path = f"{transfer_dir}/filestore.tar.gz"
                step("Transferring filestore archive…")
                _sftp_transfer_file(source_server, fs_path, target_server, dst_fs_path)
                fs_path = dst_fs_path
                step("Filestore archive transferred.")

        # ── Phase 3: Restore into the new instance ────────────────────────────
        step("Restoring backup into new instance…")

        if target_server.deployment_mode == OdooServer.DeploymentMode.BARE_METAL:
            svc = shlex.quote(new_instance.systemd_service or f"odoo-{db_name}")

            step(f"Stopping service {svc}…")
            run(f"sudo systemctl stop {svc} 2>/dev/null || true", allow_failure=True)

            step(f"Dropping database '{db_name}'…")
            run(f"sudo -u postgres dropdb --if-exists {shlex.quote(db_name)}")
            step(f"Creating database '{db_name}'…")
            run(f"sudo -u postgres createdb -O odoo {shlex.quote(db_name)}")

            step("Restoring database…")
            run(
                f"gunzip -c {db_path} | sudo -u postgres psql {shlex.quote(db_name)}",
                timeout=3600,
            )
            step("Database restored.")

            if fs_path:
                data_dir = _bare_metal_data_dir(new_instance)
                filestore_parent = f"{data_dir}/filestore"
                filestore_dir    = f"{filestore_parent}/{db_name}"
                step(f"Restoring filestore to {filestore_dir}…")
                run(f"rm -rf {shlex.quote(filestore_dir)}")
                run(f"mkdir -p {shlex.quote(filestore_parent)}")
                run(f"tar -xzf {fs_path} -C {shlex.quote(filestore_parent)}", timeout=3600)
                run(f"chown -R odoo:odoo {shlex.quote(filestore_dir)}", allow_failure=True)
                step("Filestore restored.")

            step(f"Starting service {svc}…")
            run(f"sudo systemctl start {svc}")

        else:  # DOCKER
            container = shlex.quote(new_instance.container_name or f"odoo-{db_name}")

            step(f"Stopping container {container}…")
            run(f"docker stop {container} 2>/dev/null || true", allow_failure=True)

            step(f"Dropping database '{db_name}'…")
            run(f"docker exec odoo-postgres dropdb -U odoo --if-exists {shlex.quote(db_name)}")
            step(f"Creating database '{db_name}'…")
            run(f"docker exec odoo-postgres createdb -U odoo -d postgres {shlex.quote(db_name)}")

            step("Restoring database…")
            run(
                f"gunzip -c {db_path} | docker exec -i odoo-postgres psql -U odoo {shlex.quote(db_name)}",
                timeout=3600,
            )
            step("Database restored.")

            if fs_path:
                filestore_parent = "/data/odoo"
                filestore_dir    = f"{filestore_parent}/{db_name}"
                step(f"Restoring filestore to {filestore_dir}…")
                run(f"rm -rf {shlex.quote(filestore_dir)}")
                run(f"mkdir -p {shlex.quote(filestore_parent)}")
                run(f"tar -xzf {fs_path} -C {shlex.quote(filestore_parent)}", timeout=3600)
                run(f"chown -R 100:101 {shlex.quote(filestore_dir)}", allow_failure=True)
                step("Filestore restored.")

            step(f"Starting container {container}…")
            run(f"docker start {container}")

        # Clean up transfer dir on target server if we used one
        if not same_server:
            run(f"rm -rf {transfer_dir}", allow_failure=True)

        new_instance.status = OdooInstance.Status.RUNNING
        new_instance.save(update_fields=["status", "updated_at"])

        step("Restore to new instance complete.")
        _job_done(job_id, ok=True, log=log)

    except Exception as exc:
        logger.exception(
            "restore_backup_to_new_instance failed: backup=%s new_instance=%s",
            backup_id, new_instance_id,
        )
        error_log = _log_append(log, f"ERROR: {exc}")
        new_instance.status = OdooInstance.Status.FAILED
        new_instance.save(update_fields=["status", "updated_at"])
        _job_done(job_id, ok=False, log=error_log)
