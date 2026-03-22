import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import TemplateView
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from cloud.models import CloudAccount, PyOSSSHSettings
from cloud.providers import get_provider
from cloud.pyos import looks_like_public_key_text
from deployments.models import (
    DeploymentJob,
    Infrastructure,
    Instance,
    OdooInstance,
    OdooInstanceHistory,
    OdooServer,
    OdooServerHistory,
    ServerSSHKey,
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
    deploy_server_ssh_key,
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


def _broadcast_server_event(server_id: int, payload: dict):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            f"odoo.server.{server_id}",
            {"type": "server.update", "payload": payload},
        )
    except Exception:
        logger.warning("Server broadcast skipped for server %s", server_id, exc_info=True)


def _broadcast_server_snapshot(server: OdooServer):
    """Push a full server snapshot to open websocket listeners."""
    try:
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


def _create_pyos_infrastructure(
    org,
    name: str,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: str,
    ssh_key_path: str,
    created_by,
):
    """Create the ExternalServer + Infrastructure records for a direct PYOS server."""
    from cloud.models import ExternalServer

    ext = ExternalServer(
        organization=org,
        name=name,
        host=host,
        port=port,
        username=username,
        auth_type=auth_type,
        ssh_key_path=ssh_key_path.strip(),
        is_verified=False,
        verification_error="Reachability has not been verified yet.",
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
        created_by=created_by,
    )
    return infra, ext


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
        ctx["odoo_servers"] = (
            OdooServer.objects.filter(organization=org)
            .filter(is_active=True)
            .select_related("infrastructure", "infrastructure__external_server", "cloud_account")
            .order_by("-created_at")[:100]
        )
        ctx["odoo_instances"] = (
            OdooInstance.objects.filter(organization=org, server__is_active=True)
            .select_related("server")
            .order_by("-created_at")[:20]
        )
        ctx["recent_runs"] = TerraformRun.objects.filter(
            instance__organization=org
        ).select_related("instance")[:15]
        ctx["enforcer"] = getattr(self.request, "subscription_enforcer", SubscriptionEnforcer(org))
        from cloud.models import SystemSSHKey
        ctx["dafeapp_public_key"] = SystemSSHKey.get_or_create_keypair().public_key
        ctx["pyos_default_ssh_key_path"] = PyOSSSHSettings.get_or_create_settings().default_ssh_key_path
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
        dns_domain = (request.POST.get("dns_domain") or "").strip()
        deployment_mode = (request.POST.get("deployment_mode") or "").strip()
        if deployment_mode not in (OdooServer.DeploymentMode.BARE_METAL, OdooServer.DeploymentMode.DOCKER):
            deployment_mode = OdooServer.DeploymentMode.BARE_METAL

        # Direct PYOS provisioning: one request creates the external server
        # connection, the infrastructure wrapper, and the OdooServer record.
        host = (request.POST.get("host") or "").strip()
        if host:
            port_raw = request.POST.get("port") or "22"
            username = (request.POST.get("username") or "root").strip()
            auth_type = (request.POST.get("auth_type") or "DAFEAPP_KEY").strip()
            password = request.POST.get("password") or ""
            ssh_key_path = (request.POST.get("ssh_key_path") or "").strip()
            if not name:
                return JsonResponse({"error": "name is required."}, status=400)
            if auth_type not in ("DAFEAPP_KEY", "PASSWORD"):
                return JsonResponse({"error": "auth_type must be DAFEAPP_KEY or PASSWORD."}, status=400)
            if auth_type == "PASSWORD" and not password.strip():
                return JsonResponse({"error": "Password is required for password auth."}, status=400)
            if ssh_key_path and looks_like_public_key_text(ssh_key_path):
                return JsonResponse(
                    {
                        "error": (
                            "SSH key path must be a file path on the machine running DafeApp, "
                            "not pasted public key text."
                        )
                    },
                    status=400,
                )
            try:
                port = int(port_raw)
                if not (1 <= port <= 65535):
                    raise ValueError
            except (ValueError, TypeError):
                return JsonResponse({"error": "Port must be a number between 1 and 65535."}, status=400)

            infrastructure, _ = _create_pyos_infrastructure(
                org,
                name=name,
                host=host,
                port=port,
                username=username,
                auth_type=auth_type,
                password=password,
                ssh_key_path=ssh_key_path,
                created_by=request.user,
            )
            server = OdooServer.objects.create(
                organization=org,
                infrastructure=infrastructure,
                cloud_account=None,
                name=name,
                odoo_version=odoo_version,
                region="pyos",
                size="existing-server",
                ip_address=host,
                dns_domain=dns_domain,
                deployment_mode=deployment_mode,
                created_by=request.user,
            )
            _dispatch(provision_odoo_server, server.id)
            return JsonResponse(OdooServerSerializer(server).data, status=201)

        region = (request.POST.get("region") or "").strip()
        size = (request.POST.get("size") or "").strip()
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
            deployment_mode=deployment_mode,
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
        qs = OdooServer.objects.filter(organization=org, is_active=True)
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
        server = get_object_or_404(OdooServer, pk=server_id, organization=org, is_active=True)
        return JsonResponse(OdooServerSerializer(server).data)


class PyosVpsCreateAPIView(LoginRequiredMixin, View):
    """POST — create ExternalServer + Infrastructure inline from the deployment modal."""

    def post(self, request):
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
        ssh_key_path = (request.POST.get("ssh_key_path") or "").strip()
        if ssh_key_path and looks_like_public_key_text(ssh_key_path):
            return JsonResponse(
                {
                    "error": (
                        "SSH key path must be a file path on the machine running DafeApp, "
                        "not pasted public key text."
                    )
                },
                status=400,
            )
        try:
            port = int(port_raw)
            if not (1 <= port <= 65535):
                raise ValueError
        except (ValueError, TypeError):
            return JsonResponse({"error": "Port must be a number between 1 and 65535."}, status=400)

        infra, ext = _create_pyos_infrastructure(
            org,
            name=name,
            host=host,
            port=port,
            username=username,
            auth_type=auth_type,
            password=password,
            ssh_key_path=ssh_key_path,
            created_by=request.user,
        )
        return JsonResponse({"infrastructure_id": infra.id, "external_server_id": ext.id}, status=201)


class OdooServerCheckConnectivityView(LoginRequiredMixin, View):
    """POST /odoo/servers/<server_id>/check/ — probe SSH reachability by IP/port."""

    def post(self, request, server_id):
        from django.utils import timezone
        import socket

        from deployments.tasks import _tcp_reachable

        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)

        server = get_object_or_404(
            OdooServer.objects.select_related("infrastructure", "infrastructure__external_server"),
            pk=server_id,
            organization=org,
            is_active=True,
        )

        infra = server.infrastructure
        if infra and infra.infra_type == Infrastructure.InfraType.PYOS and infra.external_server:
            ext = infra.external_server
            host = str(ext.host)
            port = ext.port or 22
            reachable = _tcp_reachable(host, port)
            message = f"Port reachable at {host}:{port}." if reachable else f"Host unreachable for {host}:{port}."
            ext.is_verified = reachable
            ext.verification_error = "" if reachable else message
            ext.last_verified_at = timezone.now()
            ext.save(update_fields=["is_verified", "verification_error", "last_verified_at"])
        elif server.ip_address:
            host = str(server.ip_address)
            port = 22
            reachable = False
            message = ""
            try:
                with socket.create_connection((host, port), timeout=5):
                    reachable = True
            except OSError as exc:
                message = str(exc)
        else:
            host = ""
            port = 22
            reachable = False
            message = "No host/IP to probe — server has no IP yet."

        now = timezone.now()
        server.is_reachable = reachable
        server.last_checked_at = now
        update_fields = ["is_reachable", "last_checked_at"]
        if host and server.ip_address != host:
            server.ip_address = host
            update_fields.append("ip_address")
        server.save(update_fields=update_fields)
        if infra and infra.infra_type == Infrastructure.InfraType.PYOS and infra.external_server:
            ext.is_reachable = reachable
            ext.last_checked_at = now
            ext.save(update_fields=["is_reachable", "last_checked_at"])

        _broadcast_server_snapshot(server)

        payload = {
            "is_reachable": reachable,
            "last_checked_at": now.isoformat(),
            "connectivity_status": "connected" if reachable else "disconnected",
        }
        if message:
            payload["message"] = message
        return JsonResponse(payload)


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
        if not server.is_active:
            return JsonResponse({"error": "Server is archived."}, status=400)
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
        qs = OdooInstance.objects.filter(
            organization=org,
            server__is_active=True,
            server__status__in=[
                OdooServer.Status.PENDING,
                OdooServer.Status.PROVISIONING,
                OdooServer.Status.CONFIGURING,
                OdooServer.Status.PROVISIONED,
                OdooServer.Status.FAILED,
            ],
        ).select_related("server")
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


class OdooServerArchiveAPIView(LoginRequiredMixin, View):
    def post(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        server = get_object_or_404(
            OdooServer.objects.select_related(
                "infrastructure",
                "infrastructure__cloud_account",
                "cloud_account",
            ),
            pk=server_id,
            organization=org,
        )

        try:
            server.is_active = False
            server.status = OdooServer.Status.ARCHIVED
            server.provisioning_log = (server.provisioning_log + "\n" + "Server archived (inactivated).").strip()
            server.save(update_fields=["is_active", "status", "provisioning_log", "updated_at"])
            _broadcast_server_event(server.id, {"type": "removed", "server_id": server.id, "reason": "archived"})
            return JsonResponse({"ok": True, "message": "Server archived and hidden from the UI."})

        except Exception as exc:
            logger.exception("Unexpected error archiving OdooServer %s", server_id)
            return JsonResponse({"error": f"Archive failed: {exc}"}, status=500)


class OdooServerDeleteAPIView(LoginRequiredMixin, View):
    def post(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        server = get_object_or_404(
            OdooServer.objects.select_related(
                "infrastructure",
                "infrastructure__cloud_account",
                "cloud_account",
            ),
            pk=server_id,
            organization=org,
        )
        try:
            with transaction.atomic():
                # Clear the child rows first so hard-delete stays predictable.
                server.instances.all().delete()
                server.history.all().delete()
                server.jobs.all().delete()
                server.ssh_keys.all().delete()
                server.delete()
            _broadcast_server_event(server_id, {"type": "removed", "server_id": server_id, "reason": "deleted"})
            return JsonResponse({"ok": True, "message": "Server deleted from the database."})
        except Exception as exc:
            logger.exception("Unexpected error deleting OdooServer %s", server_id)
            return JsonResponse({"error": f"Delete failed: {exc}"}, status=500)


class InfrastructureDeleteAPIView(LoginRequiredMixin, View):
    def post(self, request, infrastructure_id):
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        if request.org_role not in ("SUPER_ADMIN", "ADMIN"):
            return JsonResponse({"error": "Permission denied."}, status=403)

        force = str(request.POST.get("force", "")).lower() in ("1", "true", "yes")
        infra = get_object_or_404(Infrastructure, pk=infrastructure_id, organization=org)
        servers = infra.servers.all()
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
                server.delete()
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


class ServerSSHKeyListCreateAPIView(LoginRequiredMixin, View):
    """
    GET  /api/deployments/odoo/servers/<id>/ssh-keys/  — list keys for a server
    POST /api/deployments/odoo/servers/<id>/ssh-keys/  — add a new key and deploy it
    """

    def _get_server(self, request, server_id):
        org = getattr(request, "organization", None)
        if not org:
            return None, JsonResponse({"error": "No active organization."}, status=400)
        server = get_object_or_404(OdooServer, pk=server_id, organization=org)
        return server, None

    def get(self, request, server_id):
        server, err = self._get_server(request, server_id)
        if err:
            return err
        keys = server.ssh_keys.all().values("id", "label", "public_key", "deployed", "created_at")
        return JsonResponse({"keys": list(keys)})

    def post(self, request, server_id):
        import json as _json
        server, err = self._get_server(request, server_id)
        if err:
            return err

        try:
            body = _json.loads(request.body)
        except Exception:
            body = request.POST

        label = (body.get("label") or "").strip()
        public_key = (body.get("public_key") or "").strip()
        if not label:
            return JsonResponse({"error": "Label is required."}, status=400)
        if not public_key:
            return JsonResponse({"error": "Public key is required."}, status=400)
        if not (public_key.startswith("ssh-") or public_key.startswith("ecdsa-")):
            return JsonResponse({"error": "Invalid public key format."}, status=400)
        if ServerSSHKey.objects.filter(server=server, public_key=public_key).exists():
            return JsonResponse({"error": "This key is already registered on this server."}, status=400)

        key_obj = ServerSSHKey.objects.create(
            server=server,
            label=label,
            public_key=public_key,
            added_by=request.user,
        )

        if server.status == OdooServer.Status.PROVISIONED and server.ip_address:
            _dispatch(deploy_server_ssh_key, key_obj.pk)
            message = "Key added and deployment queued."
        else:
            message = "Key saved. It will be deployed when the server is provisioned."

        return JsonResponse({
            "id": key_obj.pk,
            "label": key_obj.label,
            "deployed": key_obj.deployed,
            "message": message,
        }, status=201)


class ServerSSHKeyDeleteAPIView(LoginRequiredMixin, View):
    """DELETE /api/deployments/odoo/servers/<id>/ssh-keys/<key_id>/"""

    def post(self, request, server_id, key_id):
        import json as _json
        org = getattr(request, "organization", None)
        if not org:
            return JsonResponse({"error": "No active organization."}, status=400)
        server = get_object_or_404(OdooServer, pk=server_id, organization=org)
        key_obj = get_object_or_404(ServerSSHKey, pk=key_id, server=server)
        key_obj.delete()
        return JsonResponse({"deleted": True})
