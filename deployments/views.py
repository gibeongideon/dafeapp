import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import TemplateView

from cloud.models import CloudAccount
from cloud.providers import get_provider
from deployments.models import Instance, OdooInstance, OdooServer, TerraformRun
from deployments.serializers import (
    InstanceSerializer,
    OdooInstanceSerializer,
    OdooServerSerializer,
    TerraformRunSerializer,
)
from deployments.tasks import create_odoo_instance, provision_odoo_server, terraform_apply_instance
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
        ctx["recent_runs"] = TerraformRun.objects.filter(
            instance__organization=org
        ).select_related("instance")[:15]
        ctx["enforcer"] = getattr(self.request, "subscription_enforcer", SubscriptionEnforcer(org))
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

        account = get_object_or_404(
            CloudAccount,
            pk=request.POST.get("cloud_account"),
            organization=org,
            is_verified=True,
        )
        odoo_version = (request.POST.get("odoo_version") or "").strip()
        if odoo_version not in ("18", "19"):
            return JsonResponse({"error": "odoo_version must be '18' or '19'."}, status=400)

        name = (request.POST.get("name") or "").strip()
        region = (request.POST.get("region") or "").strip()
        size = (request.POST.get("size") or "").strip()
        dns_domain = (request.POST.get("dns_domain") or "").strip()
        if not name or not region or not size:
            return JsonResponse({"error": "name, region and size are required."}, status=400)

        server = OdooServer.objects.create(
            organization=org,
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
        if version in ("18", "19"):
            qs = qs.filter(odoo_version=version)
        data = OdooServerSerializer(qs[:100], many=True).data
        return JsonResponse({"results": data})


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
        if server.status != OdooServer.Status.READY:
            return JsonResponse({"error": "Server is not READY yet."}, status=400)

        name = (request.POST.get("name") or "").strip()
        db_name = (request.POST.get("db_name") or "").strip()
        domain = (request.POST.get("domain") or "").strip()
        port = int(request.POST.get("http_port") or 8069)
        if not name or not db_name:
            return JsonResponse({"error": "name and db_name are required."}, status=400)

        inst = OdooInstance.objects.create(
            organization=org,
            server=server,
            name=name,
            db_name=db_name,
            domain=domain,
            http_port=port,
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
