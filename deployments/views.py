import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import TemplateView

from cloud.models import CloudAccount
from cloud.providers import get_provider
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
from deployments.serializers import (
    DeploymentJobSerializer,
    InfrastructureSerializer,
    InstanceSerializer,
    OdooInstanceHistorySerializer,
    OdooInstanceSerializer,
    OdooServerHistorySerializer,
    OdooServerSerializer,
    TerraformRunSerializer,
)
from deployments.tasks import (
    create_odoo_instance,
    delete_odoo_instance,
    provision_odoo_server,
    rollback_odoo_instance,
    terraform_apply_instance,
)
from subscriptions.exceptions import SubscriptionError, SubscriptionLimitError
from subscriptions.services import SubscriptionEnforcer

logger = logging.getLogger(__name__)


def _dispatch(task, *args):
    """Try async Celery dispatch; fall back to synchronous execution in dev."""
    try:
        task.delay(*args)
    except Exception:
        logger.warning("Celery broker unavailable; running task synchronously.", exc_info=True)
        task(*args)


def _active_instances_for_server(server: OdooServer):
    return server.instances.exclude(status=OdooInstance.Status.DELETED)


def _next_available_port(server: OdooServer) -> int | None:
    used = set(
        _active_instances_for_server(server).values_list("http_port", flat=True)
    )
    for port in range(server.min_port, server.max_port + 1):
        if port not in used:
            return port
    return None


def _capacity_check(server: OdooServer, cpu: int, ram_mb: int) -> tuple[bool, str]:
    active = _active_instances_for_server(server)
    count = active.count()
    if count >= server.max_instances:
        return False, f"Max instances per server reached ({count}/{server.max_instances})."
    used_cpu = sum(active.values_list("requested_cpu_cores", flat=True))
    used_ram = sum(active.values_list("requested_ram_mb", flat=True))
    if used_cpu + cpu > server.capacity_cpu_cores:
        return False, f"CPU capacity exceeded ({used_cpu + cpu}/{server.capacity_cpu_cores} cores)."
    if used_ram + ram_mb > server.capacity_ram_mb:
        return False, f"RAM capacity exceeded ({used_ram + ram_mb}/{server.capacity_ram_mb} MB)."
    return True, ""


class DeploymentCreateView(LoginRequiredMixin, TemplateView):
    template_name = "deployments/create_instance.html"

    def dispatch(self, request, *args, **kwargs):
        resp = super().dispatch(request, *args, **kwargs)
        if not request.user.is_authenticated:
            return resp
        if not getattr(request, "organization", None):
            return redirect("organizations:select")
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            messages.error(request, "You do not have permission to create instances.")
            return redirect("core:dashboard")
        return resp

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        accounts = CloudAccount.objects.filter(organization=org, is_verified=True)
        ctx["accounts"] = accounts
        from cloud.models import ExternalServer

        ctx["external_servers"] = ExternalServer.objects.filter(
            organization=org, is_verified=True
        ).order_by("-created_at")
        ctx["infrastructures"] = Infrastructure.objects.filter(
            organization=org
        ).select_related("cloud_account", "external_server")[:100]
        ctx["odoo_servers"] = OdooServer.objects.filter(
            organization=org
        ).select_related("infrastructure", "cloud_account").order_by("-created_at")[:100]
        ctx["odoo_instances"] = (
            OdooInstance.objects.filter(organization=org)
            .select_related("server")
            .order_by("-created_at")[:20]
        )
        ctx["recent_runs"] = TerraformRun.objects.filter(
            instance__organization=org
        ).select_related("instance")[:15]
        ctx["enforcer"] = getattr(self.request, "subscription_enforcer", SubscriptionEnforcer(org))
        from cloud.models import SystemSSHKey
        ctx["dafeapp_public_key"] = SystemSSHKey.get_or_create_keypair().public_key
        return ctx

    def post(self, request):
        org = request.organization
        enforcer = getattr(request, "subscription_enforcer", SubscriptionEnforcer(org))
        try:
            enforcer.ensure_active()
            enforcer.check_instance_limit()
        except (SubscriptionError, SubscriptionLimitError) as exc:
            messages.error(request, str(exc))
            return redirect("deployments:create-instance")

        account = get_object_or_404(
            CloudAccount,
            pk=request.POST.get("cloud_account"),
            organization=org,
            is_verified=True,
        )
        name = (request.POST.get("name") or "").strip()
        region = (request.POST.get("region") or "").strip()
        size = (request.POST.get("size") or "").strip()
        if not name or not region or not size:
            messages.error(request, "Name, region and size are required.")
            return redirect("deployments:create-instance")

        instance = Instance.objects.create(
            organization=org,
            cloud_account=account,
            name=name,
            region=region,
            size=size,
            status=Instance.Status.PENDING,
            created_by=request.user,
        )
        run = TerraformRun.objects.create(instance=instance, status=TerraformRun.Status.QUEUED)
        _dispatch(terraform_apply_instance, run.id)
        messages.success(request, f"Instance '{name}' queued. Run #{run.id} started.")
        return redirect("deployments:create-instance")


class CloudAccountOptionsAPIView(LoginRequiredMixin, View):
    """Provider-aware regions/sizes options for deployment form."""

    def get(self, request, account_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        account = get_object_or_404(CloudAccount, pk=account_id, organization=org, is_verified=True)
        provider = get_provider(account)
        return JsonResponse(
            {
                "regions": provider.list_regions(),
                "sizes": provider.list_sizes(),
            }
        )


class InstanceDetailAPIView(LoginRequiredMixin, View):
    def get(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        instance = get_object_or_404(Instance, pk=instance_id, organization=org)
        return JsonResponse(InstanceSerializer(instance).data)


class TerraformRunDetailAPIView(LoginRequiredMixin, View):
    def get(self, request, run_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        run = get_object_or_404(TerraformRun.objects.select_related("instance"), pk=run_id, instance__organization=org)
        return JsonResponse(TerraformRunSerializer(run).data)


class OdooServerCreateAPIView(LoginRequiredMixin, View):
    def post(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        odoo_version = (request.POST.get("odoo_version") or "").strip()
        if odoo_version not in ("17", "18", "19"):
            return JsonResponse({"error": "odoo_version must be '17', '18', or '19'."}, status=400)

        name = (request.POST.get("name") or "").strip()
        region = (request.POST.get("region") or "").strip()
        size = (request.POST.get("size") or "").strip()
        dns_domain = (request.POST.get("dns_domain") or "").strip()
        if not name or not region or not size:
            return JsonResponse({"error": "name, region and size are required."}, status=400)

        # Resolve infrastructure — accept either an existing infra id or a
        # bare cloud_account_id (auto-creates or reuses a MANAGED infrastructure).
        infra_id = (request.POST.get("infrastructure_id") or "").strip()
        account_id = (request.POST.get("cloud_account_id") or "").strip()

        if infra_id:
            infrastructure = get_object_or_404(Infrastructure, pk=infra_id, organization=org)
            ok, err = infrastructure.validate_connection_target()
            if not ok:
                return JsonResponse({"error": err}, status=400)
            account = infrastructure.managed_account
        elif account_id:
            account = get_object_or_404(CloudAccount, pk=account_id, organization=org, is_verified=True)
            # Reuse or create a MANAGED infrastructure for this account.
            infrastructure, _ = Infrastructure.objects.get_or_create(
                organization=org,
                infra_type=Infrastructure.InfraType.MANAGED,
                cloud_account=account,
                defaults={
                    "name": f"managed-{account_id}",
                    "is_connected": True,
                    "validation_log": "Auto-created by server provisioning.",
                    "created_by": request.user,
                },
            )
        else:
            return JsonResponse({"error": "Provide infrastructure_id or cloud_account_id."}, status=400)

        server = OdooServer.objects.create(
            organization=org,
            infrastructure=infrastructure,
            cloud_account=account,
            name=name,
            odoo_version=odoo_version,
            region=region,
            size=size,
            dns_domain=dns_domain,
            created_by=request.user,
        )
        _dispatch(provision_odoo_server, server.id)
        return JsonResponse(OdooServerSerializer(server).data, status=201)


class OdooServerListAPIView(LoginRequiredMixin, View):
    def get(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        version = (request.GET.get("odoo_version") or "").strip()
        qs = OdooServer.objects.filter(organization=org)
        if version in ("17", "18", "19"):
            qs = qs.filter(odoo_version=version)
        data = OdooServerSerializer(qs[:100], many=True).data
        return JsonResponse({"results": data})


class OdooServerDetailAPIView(LoginRequiredMixin, View):
    """GET /odoo/servers/<server_id>/ — poll status and provisioning_log."""

    def get(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        server = get_object_or_404(OdooServer, pk=server_id, organization=org)
        return JsonResponse(OdooServerSerializer(server).data)


class PyosVpsCreateAPIView(LoginRequiredMixin, View):
    """POST — create ExternalServer + Infrastructure inline from the deployment modal."""

    def post(self, request):
        from cloud.models import ExternalServer

        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        name = (request.POST.get("name") or "").strip()
        host = (request.POST.get("host") or "").strip()
        username = (request.POST.get("username") or "root").strip()
        auth_type = (request.POST.get("auth_type") or "DAFEAPP_KEY").strip()
        password = request.POST.get("password") or ""
        port_raw = request.POST.get("port") or "22"

        if not name or not host:
            return JsonResponse({"error": "Name and host IP are required."}, status=400)
        if auth_type not in ("DAFEAPP_KEY", "PASSWORD"):
            return JsonResponse({"error": "auth_type must be DAFEAPP_KEY or PASSWORD."}, status=400)
        if auth_type == "PASSWORD" and not password.strip():
            return JsonResponse({"error": "Password is required for password auth."}, status=400)
        try:
            port = int(port_raw)
            if not (1 <= port <= 65535):
                raise ValueError
        except (ValueError, TypeError):
            return JsonResponse({"error": "Port must be a number between 1 and 65535."}, status=400)

        ext = ExternalServer(
            organization=org,
            name=name,
            host=host,
            port=port,
            username=username,
            auth_type=auth_type,
            is_verified=True,
        )
        if auth_type == "PASSWORD":
            ext._raw_password = password.strip()
        ext.save()

        infra_name = name
        if Infrastructure.objects.filter(organization=org, name=infra_name).exists():
            infra_name = f"{name}-{ext.id}"
        infra = Infrastructure.objects.create(
            organization=org,
            name=infra_name,
            infra_type=Infrastructure.InfraType.PYOS,
            external_server=ext,
            is_connected=True,
            validation_log="Created via inline deployment form.",
            created_by=request.user,
        )
        return JsonResponse({"infrastructure_id": infra.id, "external_server_id": ext.id}, status=201)


class OdooServerCheckConnectivityView(LoginRequiredMixin, View):
    """POST /odoo/servers/<server_id>/check/ — probe SSH port and update is_reachable."""

    def post(self, request, server_id):
        import socket
        from django.utils import timezone

        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)

        server = get_object_or_404(
            OdooServer.objects.select_related("infrastructure", "infrastructure__external_server"),
            pk=server_id,
            organization=org,
        )

        infra = server.infrastructure
        if infra and infra.infra_type == Infrastructure.InfraType.PYOS and infra.external_server:
            ext = infra.external_server
            host = str(ext.host)
            port = ext.port or 22
        elif server.ip_address:
            host = str(server.ip_address)
            port = 22
        else:
            return JsonResponse({"error": "No host/IP to probe — server has no IP yet."}, status=400)

        try:
            with socket.create_connection((host, port), timeout=5):
                reachable = True
        except OSError:
            reachable = False

        now = timezone.now()
        server.is_reachable = reachable
        server.last_checked_at = now
        server.save(update_fields=["is_reachable", "last_checked_at"])

        return JsonResponse({
            "is_reachable": reachable,
            "last_checked_at": now.isoformat(),
            "connectivity_status": "connected" if reachable else "disconnected",
        })


class OdooInstanceCreateAPIView(LoginRequiredMixin, View):
    def post(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        enforcer = getattr(request, "subscription_enforcer", SubscriptionEnforcer(org))
        try:
            enforcer.ensure_active()
            enforcer.check_instance_limit()
        except (SubscriptionError, SubscriptionLimitError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        server = get_object_or_404(
            OdooServer,
            pk=request.POST.get("server_id"),
            organization=org,
        )
        if server.status != OdooServer.Status.PROVISIONED:
            return JsonResponse({"error": "Server is not PROVISIONED yet."}, status=400)
        if server.last_checked_at is not None and not server.is_reachable:
            return JsonResponse(
                {"error": "Server SSH is unreachable. Click Check on the server card before creating instances."},
                status=400,
            )

        name = (request.POST.get("name") or "").strip()
        db_name = (request.POST.get("db_name") or "").strip()
        domain = (request.POST.get("domain") or "").strip()
        req_cpu = int(request.POST.get("requested_cpu_cores") or 1)
        req_ram = int(request.POST.get("requested_ram_mb") or 1024)
        port_raw = request.POST.get("http_port")
        if not name or not db_name:
            return JsonResponse({"error": "name and db_name are required."}, status=400)
        if domain and OdooInstance.objects.filter(organization=org, domain=domain).exclude(status=OdooInstance.Status.DELETED).exists():
            return JsonResponse({"error": "Domain is already used by another instance."}, status=400)

        port = int(port_raw) if port_raw else _next_available_port(server)
        if port is None:
            return JsonResponse({"error": "No available port on this server."}, status=400)
        if port < server.min_port or port > server.max_port:
            return JsonResponse({"error": f"Port must be within {server.min_port}-{server.max_port}."}, status=400)
        if _active_instances_for_server(server).filter(http_port=port).exists():
            return JsonResponse({"error": "Selected port is already in use on this server."}, status=400)

        ok, capacity_msg = _capacity_check(server, req_cpu, req_ram)
        if not ok:
            return JsonResponse({"error": capacity_msg}, status=400)

        inst = OdooInstance.objects.create(
            organization=org,
            server=server,
            name=name,
            db_name=db_name,
            domain=domain,
            http_port=port,
            requested_cpu_cores=req_cpu,
            requested_ram_mb=req_ram,
            created_by=request.user,
        )
        _dispatch(create_odoo_instance, inst.id)
        return JsonResponse(OdooInstanceSerializer(inst).data, status=201)


class OdooInstanceListAPIView(LoginRequiredMixin, View):
    def get(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        server_id = request.GET.get("server_id")
        qs = OdooInstance.objects.filter(organization=org).select_related("server")
        if server_id:
            qs = qs.filter(server_id=server_id)
        data = OdooInstanceSerializer(qs[:200], many=True).data
        return JsonResponse({"results": data})


class OdooInstanceConsoleView(LoginRequiredMixin, TemplateView):
    template_name = "deployments/odoo_instance_console.html"

    def dispatch(self, request, *args, **kwargs):
        resp = super().dispatch(request, *args, **kwargs)
        if not request.user.is_authenticated:
            return resp
        if not getattr(request, "organization", None):
            return redirect("organizations:select")
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER", "USER"):
            messages.error(request, "You do not have permission to open this instance.")
            return redirect("core:dashboard")
        return resp

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=self.kwargs["instance_id"],
            organization=org,
        )
        ctx["odoo_instance"] = instance
        ctx["odoo_server"] = instance.server
        ctx["env_sections"] = ["Production", "Staging", "Development"]
        ctx["tool_tabs"] = [
            "GitHistory",
            "Shell",
            "Monitor",
            "logs",
            "backups",
            "Upgrade",
            "tools",
            "setting",
        ]
        return ctx


class InfrastructureCreateAPIView(LoginRequiredMixin, View):
    def post(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        name = (request.POST.get("name") or "").strip()
        infra_type = (request.POST.get("infra_type") or "").strip()
        if infra_type not in (Infrastructure.InfraType.PYOS, Infrastructure.InfraType.MANAGED):
            return JsonResponse({"error": "infra_type must be PYOS or MANAGED."}, status=400)
        if not name:
            return JsonResponse({"error": "name is required."}, status=400)

        ext = None
        account = None
        if infra_type == Infrastructure.InfraType.PYOS:
            from cloud.models import ExternalServer
            ext = get_object_or_404(ExternalServer, pk=request.POST.get("external_server_id"), organization=org)
            if not ext.is_verified:
                return JsonResponse({"error": "PYOS infrastructure requires a verified external server."}, status=400)
        else:
            account = get_object_or_404(CloudAccount, pk=request.POST.get("cloud_account_id"), organization=org, is_verified=True)

        infra = Infrastructure.objects.create(
            organization=org,
            name=name,
            infra_type=infra_type,
            external_server=ext,
            cloud_account=account,
            is_connected=True,
            validation_log="Validated at creation.",
            created_by=request.user,
        )
        ok, err = infra.validate_connection_target()
        if not ok:
            infra.delete()
            return JsonResponse({"error": err}, status=400)
        return JsonResponse(InfrastructureSerializer(infra).data, status=201)


class InfrastructureListAPIView(LoginRequiredMixin, View):
    def get(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        data = InfrastructureSerializer(
            Infrastructure.objects.filter(organization=org)[:100], many=True
        ).data
        return JsonResponse({"results": data})


class OdooInstanceDeleteAPIView(LoginRequiredMixin, View):
    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)
        instance = get_object_or_404(OdooInstance, pk=instance_id, organization=org)
        if instance.status == OdooInstance.Status.DELETED:
            return JsonResponse({"ok": True, "message": "Instance already deleted."})
        # Dispatch async cleanup (stop service, drop DB, free port).
        _dispatch(delete_odoo_instance, instance.id)
        return JsonResponse({"ok": True, "message": "Instance deletion queued."})


class OdooServerDeleteAPIView(LoginRequiredMixin, View):
    def post(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        server = get_object_or_404(
            OdooServer.objects.select_related("infrastructure", "cloud_account"),
            pk=server_id,
            organization=org,
        )
        for inst in server.instances.exclude(status=OdooInstance.Status.DELETED):
            inst.status = OdooInstance.Status.DELETED
            inst.provisioning_log = (inst.provisioning_log + "\n" + "Deleted due to server deletion.").strip()
            inst.save(update_fields=["status", "provisioning_log", "updated_at"])

        infra_type = server.infrastructure.infra_type if server.infrastructure else (
            Infrastructure.InfraType.MANAGED if server.cloud_account else Infrastructure.InfraType.PYOS
        )
        if infra_type == Infrastructure.InfraType.MANAGED and server.provider_server_id and server.cloud_account:
            try:
                provider = get_provider(server.cloud_account)
                provider.destroy_server(server.provider_server_id)
            except Exception:
                pass

        server.status = OdooServer.Status.DELETED
        server.provisioning_log = (server.provisioning_log + "\n" + "Server deleted.").strip()
        server.save(update_fields=["status", "provisioning_log", "updated_at"])
        return JsonResponse({"ok": True, "message": "Server and child instances deleted."})


class InfrastructureDeleteAPIView(LoginRequiredMixin, View):
    def post(self, request, infrastructure_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        force = str(request.POST.get("force", "")).lower() in ("1", "true", "yes")
        infra = get_object_or_404(Infrastructure, pk=infrastructure_id, organization=org)
        servers = infra.servers.exclude(status=OdooServer.Status.DELETED)
        if servers.exists() and not force:
            return JsonResponse(
                {"error": "Infrastructure has servers. Set force=true to delete recursively."},
                status=400,
            )
        if force:
            for server in servers:
                for inst in server.instances.exclude(status=OdooInstance.Status.DELETED):
                    inst.status = OdooInstance.Status.DELETED
                    inst.provisioning_log = (inst.provisioning_log + "\n" + "Deleted due to infrastructure force delete.").strip()
                    inst.save(update_fields=["status", "provisioning_log", "updated_at"])
                server.status = OdooServer.Status.DELETED
                server.provisioning_log = (server.provisioning_log + "\n" + "Deleted due to infrastructure force delete.").strip()
                server.save(update_fields=["status", "provisioning_log", "updated_at"])
        infra.delete()
        return JsonResponse({"ok": True, "message": "Infrastructure deleted."})


# ---------------------------------------------------------------------------
# Phase 2: Deployment Jobs, History, Health Check, Rollback
# ---------------------------------------------------------------------------

class DeploymentJobListAPIView(LoginRequiredMixin, View):
    """GET /deployments/jobs/ — list recent deployment jobs for the active org."""

    def get(self, request):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        qs = DeploymentJob.objects.filter(organization=org).order_by("-created_at")
        instance_id = request.GET.get("instance_id")
        server_id = request.GET.get("server_id")
        if instance_id:
            qs = qs.filter(odoo_instance_id=instance_id)
        if server_id:
            qs = qs.filter(odoo_server_id=server_id)
        return JsonResponse({"results": DeploymentJobSerializer(qs[:100], many=True).data})


class DeploymentJobCancelAPIView(LoginRequiredMixin, View):
    """POST /deployments/jobs/<id>/cancel/ — revoke a running Celery task and mark it cancelled."""

    def post(self, request, job_id):
        from celery import current_app

        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        job = get_object_or_404(DeploymentJob, pk=job_id, organization=org)
        if job.status not in (DeploymentJob.Status.QUEUED, DeploymentJob.Status.RUNNING):
            return JsonResponse({"error": f"Job is already {job.status}."}, status=400)

        if job.celery_task_id:
            try:
                current_app.control.revoke(job.celery_task_id, terminate=True, signal="SIGTERM")
            except Exception:
                logger.warning("Could not revoke Celery task %s", job.celery_task_id, exc_info=True)

        from django.utils import timezone
        job.status = DeploymentJob.Status.CANCELLED
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at", "updated_at"])
        return JsonResponse({"ok": True, "status": job.status})


class OdooServerHistoryAPIView(LoginRequiredMixin, View):
    """GET /deployments/odoo/servers/<id>/history/ — deployment history for a server."""

    def get(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        server = get_object_or_404(OdooServer, pk=server_id, organization=org)
        qs = OdooServerHistory.objects.filter(server=server).order_by("-deployed_at")
        return JsonResponse({"results": OdooServerHistorySerializer(qs, many=True).data})


class OdooInstanceHistoryAPIView(LoginRequiredMixin, View):
    """GET /deployments/odoo/instances/<id>/history/ — deployment history for an instance."""

    def get(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        instance = get_object_or_404(OdooInstance, pk=instance_id, organization=org)
        qs = OdooInstanceHistory.objects.filter(instance=instance).order_by("-deployed_at")
        return JsonResponse({"results": OdooInstanceHistorySerializer(qs, many=True).data})


class OdooInstanceRollbackAPIView(LoginRequiredMixin, View):
    """POST /deployments/odoo/instances/<id>/rollback/ — re-deploy from a history snapshot."""

    def post(self, request, instance_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN", "MANAGER"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        instance = get_object_or_404(OdooInstance, pk=instance_id, organization=org)
        history_id = request.POST.get("history_id")
        if not history_id:
            return JsonResponse({"error": "history_id is required."}, status=400)
        snap = get_object_or_404(OdooInstanceHistory, pk=history_id, instance=instance)

        job = DeploymentJob.objects.create(
            organization=org,
            job_type=DeploymentJob.JobType.ROLLBACK_INSTANCE,
            odoo_instance=instance,
            created_by=request.user,
        )
        _dispatch(rollback_odoo_instance, instance.id, snap.id, job.id)
        return JsonResponse({"ok": True, "job_id": job.id, "history_id": snap.id})


class OdooInstanceHealthCheckView(LoginRequiredMixin, View):
    """POST /deployments/odoo/instances/<id>/health/ — manual HTTP health probe."""

    def post(self, request, instance_id):
        import urllib.error
        import urllib.request
        from django.utils import timezone

        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)

        instance = get_object_or_404(
            OdooInstance.objects.select_related("server"),
            pk=instance_id,
            organization=org,
        )
        if not instance.server.ip_address:
            return JsonResponse({"error": "Server has no IP yet."}, status=400)

        url = f"http://{instance.server.ip_address}:{instance.http_port}/web/health"
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                reachable = resp.status == 200
        except Exception:
            reachable = False

        now = timezone.now()
        instance.is_reachable = reachable
        instance.last_health_check = now
        instance.save(update_fields=["is_reachable", "last_health_check"])
        return JsonResponse({
            "is_reachable": reachable,
            "last_health_check": now.isoformat(),
            "url_probed": url,
        })
