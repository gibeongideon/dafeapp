import logging
import os
import posixpath
import shlex
import tempfile
import zipfile
from contextlib import suppress
from datetime import datetime, timezone as dt_timezone

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.views import View

from backups.models import OdooInstanceBackup
from backups.serializers import OdooInstanceBackupSerializer
from backups.tasks import backup_odoo_instance, restore_backup_to_new_instance, restore_odoo_instance
from deployments.models import DeploymentJob, OdooInstance, OdooServer
from deployments.tasks import _connect_ssh_client, _ssh_run

logger = logging.getLogger(__name__)

_ALLOWED_ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER")


def _dispatch(task, *args):
    """Try async Celery dispatch; fall back to synchronous if broker unavailable."""
    try:
        task.delay(*args)
    except Exception:
        logger.warning("Celery broker unavailable; running backup task synchronously.", exc_info=True)
        task(*args)


def _timestamp() -> str:
    return datetime.now(dt_timezone.utc).strftime("%Y%m%d_%H%M%S")


def _analyze_uploaded_backup_zip(archive_path: str) -> dict:
    """Validate an uploaded backup ZIP and locate db.sql.gz / filestore.tar.gz inside it."""
    with zipfile.ZipFile(archive_path) as archive:
        members = [info.filename for info in archive.infolist() if not info.is_dir()]

    if not members:
        raise ValueError("The uploaded ZIP archive is empty.")

    normalized = []
    for member in members:
        clean = posixpath.normpath(member or "").lstrip("/")
        if clean in ("", ".") or clean.startswith("../") or "/../" in f"/{clean}":
            raise ValueError("The uploaded ZIP archive contains unsafe paths.")
        normalized.append(clean)

    db_members = [member for member in normalized if posixpath.basename(member) == "db.sql.gz"]
    if len(db_members) != 1:
        raise ValueError("The uploaded ZIP must contain exactly one db.sql.gz file.")

    db_member = db_members[0]
    backup_root = posixpath.dirname(db_member)
    fs_members = [
        member for member in normalized
        if posixpath.basename(member) == "filestore.tar.gz" and posixpath.dirname(member) == backup_root
    ]
    if len(fs_members) > 1:
        raise ValueError("The uploaded ZIP contains multiple filestore.tar.gz files.")

    return {
        "backup_root": backup_root,
        "db_member": db_member,
        "filestore_member": fs_members[0] if fs_members else "",
    }


class InstanceBackupListAPIView(LoginRequiredMixin, View):
    """GET /api/backups/instances/{instance_id}/ — list backups for an instance."""

    def get(self, request, instance_id):
        if not getattr(request, "organization", None):
            return JsonResponse({"error": "No active organization."}, status=400)

        instance = get_object_or_404(
            OdooInstance, pk=instance_id, organization=request.organization
        )
        backups = OdooInstanceBackup.objects.filter(instance=instance).order_by("-created_at")[:50]
        return JsonResponse({"results": OdooInstanceBackupSerializer(backups, many=True).data})


class CreateBackupAPIView(LoginRequiredMixin, View):
    """POST /api/backups/instances/{instance_id}/backup/ — trigger a new backup."""

    def post(self, request, instance_id):
        if not getattr(request, "organization", None):
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in _ALLOWED_ROLES:
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance, pk=instance_id, organization=request.organization
        )
        if instance.status != OdooInstance.Status.RUNNING:
            return JsonResponse(
                {"error": f"Instance must be RUNNING to create a backup (current: {instance.status})."},
                status=400,
            )

        job = DeploymentJob.objects.create(
            organization=request.organization,
            job_type=DeploymentJob.JobType.BACKUP_INSTANCE,
            odoo_instance=instance,
            odoo_server=instance.server,
            created_by=request.user,
        )
        _dispatch(backup_odoo_instance, instance.pk, job.pk)
        return JsonResponse({"ok": True, "job_id": job.pk}, status=202)


class DownloadBackupAPIView(LoginRequiredMixin, View):
    """GET /api/backups/instances/{instance_id}/download/{backup_id}/ — stream a backup archive."""

    def get(self, request, instance_id, backup_id):
        if not getattr(request, "organization", None):
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in _ALLOWED_ROLES:
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=request.organization,
        )
        backup = get_object_or_404(
            OdooInstanceBackup,
            pk=backup_id,
            instance=instance,
            organization=request.organization,
        )
        if backup.status != OdooInstanceBackup.Status.DONE:
            return JsonResponse(
                {"error": f"Backup is not ready for download (status: {backup.status})."},
                status=400,
            )
        if not backup.backup_dir:
            return JsonResponse({"error": "Backup directory is unavailable."}, status=400)

        exists_code, _ = _ssh_run(
            instance.server,
            f"test -d {shlex.quote(backup.backup_dir)}",
            timeout=30,
        )
        if exists_code != 0:
            return JsonResponse({"error": "Backup directory was not found on the server."}, status=404)

        backup_dir = backup.backup_dir.rstrip("/")
        parent_dir = posixpath.dirname(backup_dir)
        timestamp = backup.created_at.strftime("%Y%m%d_%H%M%S")
        archive_name = f"{instance.db_name}_backup_{timestamp}.zip"

        client = None
        tmp_key = None
        try:
            client, tmp_key = _connect_ssh_client(instance.server)
            script = """
import os
import sys
import zipfile

root = sys.argv[1].rstrip("/")
base = os.path.dirname(root)

with zipfile.ZipFile(sys.stdout.buffer, "w", zipfile.ZIP_DEFLATED) as zf:
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            zf.write(full, os.path.relpath(full, base))
"""
            command = f"python3 -c {shlex.quote(script)} {shlex.quote(backup_dir)}"
            stdin, stdout, stderr = client.exec_command(f"bash -lc {shlex.quote(command)}", timeout=3600)
            stdout.channel.settimeout(3600)
        except Exception as exc:
            if client is not None:
                client.close()
            if tmp_key:
                with suppress(OSError):
                    os.unlink(tmp_key)
            logger.warning("backup download setup failed for backup %s", backup.pk, exc_info=True)
            return JsonResponse({"error": f"Unable to open backup download: {exc}"}, status=502)

        def stream():
            try:
                while True:
                    chunk = stdout.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk

                exit_status = stdout.channel.recv_exit_status()
                if exit_status != 0:
                    err_output = stderr.read().decode(errors="replace").strip()
                    logger.warning(
                        "backup download command failed for backup %s (exit %s): %s",
                        backup.pk,
                        exit_status,
                        err_output,
                    )
            finally:
                for handle in (stdin, stdout, stderr):
                    with suppress(Exception):
                        handle.close()
                if client is not None:
                    client.close()
                if tmp_key:
                    with suppress(OSError):
                        os.unlink(tmp_key)

        response = StreamingHttpResponse(stream(), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="{archive_name}"'
        return response


class UploadRestoreBackupAPIView(LoginRequiredMixin, View):
    """POST /api/backups/instances/{instance_id}/restore/upload/ — upload a ZIP and restore from it."""

    def post(self, request, instance_id):
        if not getattr(request, "organization", None):
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in _ALLOWED_ROLES:
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=request.organization,
        )
        uploaded_file = request.FILES.get("archive") or request.FILES.get("file") or request.FILES.get("backup_zip")
        if not uploaded_file:
            return JsonResponse({"error": "Choose a ZIP backup file to restore."}, status=400)

        filename = (uploaded_file.name or "").lower()
        if not filename.endswith(".zip"):
            return JsonResponse({"error": "The uploaded file must be a .zip backup archive."}, status=400)

        local_tmp_path = None
        client = None
        sftp = None
        tmp_key = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as handle:
                for chunk in uploaded_file.chunks():
                    handle.write(chunk)
                local_tmp_path = handle.name

            analyzed = _analyze_uploaded_backup_zip(local_tmp_path)
            backup_ts = _timestamp()
            remote_root = f"/backups/dafeapp/.uploaded/{instance.db_name}/{backup_ts}"
            remote_archive_path = f"{remote_root}/upload.zip"
            remote_extract_dir = f"{remote_root}/contents"
            remote_backup_dir = (
                f"{remote_extract_dir}/{analyzed['backup_root']}"
                if analyzed["backup_root"] else remote_extract_dir
            )
            remote_db_path = f"{remote_extract_dir}/{analyzed['db_member']}"
            remote_fs_path = (
                f"{remote_extract_dir}/{analyzed['filestore_member']}"
                if analyzed["filestore_member"] else ""
            )

            code, out = _ssh_run(instance.server, f"mkdir -p {shlex.quote(remote_extract_dir)}", timeout=60)
            if code != 0:
                raise RuntimeError(f"Could not prepare remote upload folder: {out}")

            client, tmp_key = _connect_ssh_client(instance.server)
            sftp = client.open_sftp()
            sftp.put(local_tmp_path, remote_archive_path)
            sftp.close()
            sftp = None

            extract_script = """
import os
import shutil
import sys
import zipfile

archive_path, dest = sys.argv[1], sys.argv[2]
os.makedirs(dest, exist_ok=True)
base = os.path.realpath(dest) + os.sep

with zipfile.ZipFile(archive_path) as zf:
    for info in zf.infolist():
        if info.is_dir():
            continue
        target = os.path.realpath(os.path.join(dest, info.filename))
        if not target.startswith(base):
            raise SystemExit(f"unsafe path: {info.filename}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
"""
            extract_command = (
                f"python3 -c {shlex.quote(extract_script)} "
                f"{shlex.quote(remote_archive_path)} {shlex.quote(remote_extract_dir)}"
            )
            code, out = _ssh_run(instance.server, extract_command, timeout=3600)
            if code != 0:
                raise RuntimeError(f"Could not extract uploaded backup ZIP: {out}")

            _ssh_run(instance.server, f"rm -f {shlex.quote(remote_archive_path)}", timeout=60)

            backup = OdooInstanceBackup.objects.create(
                organization=request.organization,
                instance=instance,
                backup_type=OdooInstanceBackup.BackupType.FULL,
                status=OdooInstanceBackup.Status.DONE,
                backup_dir=remote_backup_dir,
                db_backup_path=remote_db_path,
                filestore_backup_path=remote_fs_path,
                size_bytes=getattr(uploaded_file, "size", 0) or 0,
                note=f"Uploaded ZIP restore: {uploaded_file.name}"[:255],
                created_by=request.user,
            )

            job = DeploymentJob.objects.create(
                organization=request.organization,
                job_type=DeploymentJob.JobType.RESTORE_INSTANCE,
                odoo_instance=instance,
                odoo_server=instance.server,
                created_by=request.user,
            )
            _dispatch(restore_odoo_instance, instance.pk, backup.pk, job.pk)
            return JsonResponse({"ok": True, "job_id": job.pk, "backup_id": backup.pk}, status=202)
        except zipfile.BadZipFile:
            return JsonResponse({"error": "The uploaded file must be a valid .zip backup archive."}, status=400)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception as exc:
            logger.warning("backup upload restore failed for instance %s", instance.pk, exc_info=True)
            return JsonResponse({"error": f"Could not upload and restore backup ZIP: {exc}"}, status=502)
        finally:
            if sftp is not None:
                with suppress(Exception):
                    sftp.close()
            if client is not None:
                with suppress(Exception):
                    client.close()
            if tmp_key:
                with suppress(OSError):
                    os.unlink(tmp_key)
            if local_tmp_path:
                with suppress(OSError):
                    os.unlink(local_tmp_path)


class RestoreBackupAPIView(LoginRequiredMixin, View):
    """POST /api/backups/instances/{instance_id}/restore/{backup_id}/ — restore from a backup."""

    def post(self, request, instance_id, backup_id):
        if not getattr(request, "organization", None):
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in _ALLOWED_ROLES:
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(
            OdooInstance, pk=instance_id, organization=request.organization
        )
        backup = get_object_or_404(
            OdooInstanceBackup,
            pk=backup_id,
            instance=instance,
            organization=request.organization,
        )
        if backup.status != OdooInstanceBackup.Status.DONE:
            return JsonResponse(
                {"error": f"Backup is not ready for restore (status: {backup.status})."},
                status=400,
            )

        job = DeploymentJob.objects.create(
            organization=request.organization,
            job_type=DeploymentJob.JobType.RESTORE_INSTANCE,
            odoo_instance=instance,
            odoo_server=instance.server,
            created_by=request.user,
        )
        _dispatch(restore_odoo_instance, instance.pk, backup.pk, job.pk)
        return JsonResponse({"ok": True, "job_id": job.pk}, status=202)


class RestoreToNewInstanceAPIView(LoginRequiredMixin, View):
    """
    POST /api/backups/instances/{instance_id}/restore-to-new/{backup_id}/

    Provisions a brand-new OdooInstance on a target server, then restores the
    backup into it. The target server can be different from the source server.

    Request body (JSON):
        server_id    (int, required)  — target OdooServer pk
        name         (str, required)  — display name for the new instance
        db_name      (str, required)  — PostgreSQL database name for the new instance
        http_port    (int, required)  — HTTP port on the target server
    """

    def post(self, request, instance_id, backup_id):
        if not getattr(request, "organization", None):
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in _ALLOWED_ROLES:
            return JsonResponse({"error": "Permission denied."}, status=403)

        # Validate source instance and backup
        source_instance = get_object_or_404(
            OdooInstance, pk=instance_id, organization=request.organization
        )
        backup = get_object_or_404(
            OdooInstanceBackup,
            pk=backup_id,
            instance=source_instance,
            organization=request.organization,
        )
        if backup.status != OdooInstanceBackup.Status.DONE:
            return JsonResponse(
                {"error": f"Backup is not ready for restore (status: {backup.status})."},
                status=400,
            )

        # Parse and validate request body
        import json
        try:
            body = json.loads(request.body or "{}")
        except (ValueError, TypeError):
            body = {}

        server_id = body.get("server_id")
        name      = (body.get("name") or "").strip()
        db_name   = (body.get("db_name") or "").strip()
        http_port = body.get("http_port")

        if not server_id:
            return JsonResponse({"error": "server_id is required."}, status=400)
        if not name:
            return JsonResponse({"error": "name is required."}, status=400)
        if not db_name:
            return JsonResponse({"error": "db_name is required."}, status=400)
        if not http_port:
            return JsonResponse({"error": "http_port is required."}, status=400)

        try:
            http_port = int(http_port)
        except (ValueError, TypeError):
            return JsonResponse({"error": "http_port must be an integer."}, status=400)

        # Validate target server belongs to this org and is provisioned
        target_server = get_object_or_404(
            OdooServer,
            pk=server_id,
            organization=request.organization,
            is_active=True,
        )
        if target_server.status != OdooServer.Status.PROVISIONED:
            return JsonResponse(
                {"error": f"Target server is not provisioned (status: {target_server.status})."},
                status=400,
            )
        if target_server.odoo_version != source_instance.server.odoo_version:
            return JsonResponse(
                {
                    "error": (
                        f"Target server runs Odoo {target_server.odoo_version} but the "
                        f"backup is from Odoo {source_instance.server.odoo_version}. "
                        "Versions must match."
                    )
                },
                status=400,
            )

        # Check db_name not already in use on target server
        if OdooInstance.objects.filter(
            server=target_server, db_name=db_name
        ).exclude(status=OdooInstance.Status.DELETED).exists():
            return JsonResponse(
                {"error": f"Database name '{db_name}' is already in use on that server."},
                status=400,
            )

        # Create the new instance record (PENDING — task will drive it)
        new_instance = OdooInstance.objects.create(
            organization=request.organization,
            server=target_server,
            name=name,
            db_name=db_name,
            http_port=http_port,
            status=OdooInstance.Status.PENDING,
            created_by=request.user,
        )

        job = DeploymentJob.objects.create(
            organization=request.organization,
            job_type=DeploymentJob.JobType.RESTORE_INSTANCE,
            odoo_instance=new_instance,
            odoo_server=target_server,
            created_by=request.user,
        )
        _dispatch(restore_backup_to_new_instance, new_instance.pk, backup.pk, job.pk)
        return JsonResponse(
            {"ok": True, "job_id": job.pk, "new_instance_id": new_instance.pk},
            status=202,
        )
