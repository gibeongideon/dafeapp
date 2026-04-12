import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from backups.models import OdooInstanceBackup
from backups.serializers import OdooInstanceBackupSerializer
from backups.tasks import backup_odoo_instance, restore_backup_to_new_instance, restore_odoo_instance
from deployments.models import DeploymentJob, OdooInstance, OdooServer

logger = logging.getLogger(__name__)

_ALLOWED_ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER")


def _dispatch(task, *args):
    """Try async Celery dispatch; fall back to synchronous if broker unavailable."""
    try:
        task.delay(*args)
    except Exception:
        logger.warning("Celery broker unavailable; running backup task synchronously.", exc_info=True)
        task(*args)


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
