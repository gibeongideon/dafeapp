import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import TemplateView, View

from audit.models import AuditLog
from cloud.forms import CloudAccountForm, ExternalServerForm, ProvisionDropletForm
from cloud.models import CloudAccount, CloudServer, ExternalServer, Infrastructure
from cloud.providers import get_provider
from core.utils import log_audit

logger = logging.getLogger(__name__)


def _dispatch(task, *args):
    """Try async Celery dispatch; fall back to synchronous if broker is unavailable."""
    try:
        task.delay(*args)
    except Exception:
        task(*args)


class CloudSuperAdminMixin(LoginRequiredMixin):
    """All cloud views require login + SUPER_ADMIN role."""

    def dispatch(self, request, *args, **kwargs):
        resp = super().dispatch(request, *args, **kwargs)
        if not request.user.is_authenticated:
            return resp
        if not request.organization:
            return redirect("organizations:select")
        if request.org_role != "SUPER_ADMIN":
            messages.error(request, "Only Super Admins can manage infrastructure.")
            return redirect("core:dashboard")
        return resp


# ── Dashboard ───────────────────────────────────────────────────────────────

class CloudDashboardView(CloudSuperAdminMixin, TemplateView):
    template_name = "cloud/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        ctx["external_servers"] = ExternalServer.objects.filter(organization=org)
        ctx["cloud_accounts"] = CloudAccount.objects.filter(organization=org)
        ctx["cloud_servers"] = CloudServer.objects.filter(
            organization=org
        ).exclude(status=CloudServer.Status.DELETED)
        return ctx


# ── External (PYOS) servers ─────────────────────────────────────────────────

class AddExternalServerView(CloudSuperAdminMixin, View):
    template_name = "cloud/add_server.html"

    def get(self, request):
        return render(request, self.template_name, {"form": ExternalServerForm()})

    def post(self, request):
        form = ExternalServerForm(request.POST)
        if form.is_valid():
            server = form.save(commit=False)
            server.organization = request.organization
            server.save()

            # Create an Infrastructure record for this server
            Infrastructure.objects.create(
                organization=request.organization,
                infra_type=Infrastructure.InfraType.PYOS,
                external_server=server,
                name=server.name,
                is_ready=False,
            )

            log_audit(
                request.user,
                AuditLog.Action.SERVER_ADD,
                request,
                f"Added PYOS server '{server.name}' ({server.host})",
            )
            messages.success(request, f"Server '{server.name}' added. Run verification to test the connection.")
            return redirect("cloud:server-detail", pk=server.pk)

        return render(request, self.template_name, {"form": form})


class ServerDetailView(CloudSuperAdminMixin, TemplateView):
    template_name = "cloud/server_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        server = get_object_or_404(
            ExternalServer, pk=self.kwargs["pk"], organization=self.request.organization
        )
        ctx["server"] = server
        return ctx


class VerifyServerView(CloudSuperAdminMixin, View):
    """Trigger async SSH validation (HTMX-friendly)."""

    def post(self, request, pk):
        server = get_object_or_404(ExternalServer, pk=pk, organization=request.organization)
        from cloud.tasks import validate_external_server
        _dispatch(validate_external_server, server.pk)

        if request.headers.get("HX-Request"):
            return HttpResponse(
                '<span class="text-gray-500 text-sm">Validation queued…</span>'
            )
        messages.info(request, "SSH validation queued.")
        return redirect("cloud:server-detail", pk=pk)


class PrepareServerView(CloudSuperAdminMixin, View):
    """Trigger async Docker/UFW preparation (HTMX-friendly)."""

    def post(self, request, pk):
        server = get_object_or_404(ExternalServer, pk=pk, organization=request.organization)
        if not server.is_verified:
            messages.error(request, "Server must be verified before preparation.")
            return redirect("cloud:server-detail", pk=pk)

        from cloud.tasks import prepare_external_server
        _dispatch(prepare_external_server, server.pk)

        if request.headers.get("HX-Request"):
            return HttpResponse(
                '<span class="text-gray-500 text-sm">Preparation queued…</span>'
            )
        messages.info(request, "Server preparation queued.")
        return redirect("cloud:server-detail", pk=pk)


# ── Cloud accounts (DigitalOcean) ────────────────────────────────────────────

class AddCloudAccountView(CloudSuperAdminMixin, View):
    template_name = "cloud/add_account.html"

    def get(self, request):
        return render(request, self.template_name, {"form": CloudAccountForm()})

    def post(self, request):
        form = CloudAccountForm(request.POST)
        if form.is_valid():
            account = form.save(commit=False)
            account.organization = request.organization
            account.save()

            log_audit(
                request.user,
                AuditLog.Action.CLOUD_ACCT_ADD,
                request,
                f"Added cloud account '{account.name}' ({account.get_provider_display()})",
            )
            messages.success(request, f"Account '{account.name}' added. Verifying token…")

            # Trigger async verification
            from cloud.tasks import validate_cloud_account
            _dispatch(validate_cloud_account, account.pk)

            return redirect("cloud:dashboard")

        return render(request, self.template_name, {"form": form})


class VerifyAccountView(CloudSuperAdminMixin, View):
    """Re-trigger token verification (HTMX-friendly)."""

    def post(self, request, pk):
        account = get_object_or_404(CloudAccount, pk=pk, organization=request.organization)
        from cloud.tasks import validate_cloud_account
        _dispatch(validate_cloud_account, account.pk)

        if request.headers.get("HX-Request"):
            return HttpResponse(
                '<span class="text-gray-500 text-sm">Verification queued…</span>'
            )
        messages.info(request, "Token verification queued.")
        return redirect("cloud:dashboard")


class CloudAccountOptionsView(CloudSuperAdminMixin, View):
    """Return region/size options for a verified account."""

    def get(self, request, pk):
        account = get_object_or_404(CloudAccount, pk=pk, organization=request.organization)
        if not account.is_verified:
            return JsonResponse(
                {"regions": [], "sizes": [], "error": "Account is not verified yet."},
                status=400,
            )
        try:
            provider = get_provider(account)
            regions = provider.list_regions()
            sizes = provider.list_sizes()
            return JsonResponse({"regions": regions, "sizes": sizes, "provider": account.provider})
        except Exception as exc:
            return JsonResponse(
                {"regions": [], "sizes": [], "error": str(exc)},
                status=400,
            )


# ── Droplet provisioning ─────────────────────────────────────────────────────

class ProvisionDropletView(CloudSuperAdminMixin, View):
    template_name = "cloud/provision_droplet.html"

    def get(self, request):
        form = ProvisionDropletForm(organization=request.organization)
        return render(request, self.template_name, {"form": form})

    def post(self, request):
        form = ProvisionDropletForm(request.POST, organization=request.organization)
        if form.is_valid():
            cloud_server = form.save(commit=False)
            cloud_server.organization = request.organization
            cloud_server.status = CloudServer.Status.PENDING
            cloud_server.save()

            log_audit(
                request.user,
                AuditLog.Action.DROPLET_PROVISION,
                request,
                f"Provisioning droplet '{cloud_server.name}' in {cloud_server.region}",
            )
            messages.success(request, f"Droplet '{cloud_server.name}' queued for provisioning.")

            from cloud.tasks import provision_do_server
            _dispatch(provision_do_server, cloud_server.pk)

            return redirect("cloud:dashboard")

        return render(request, self.template_name, {"form": form})


class DestroyDropletView(CloudSuperAdminMixin, View):
    """POST-only: destroy a CloudServer droplet."""

    def post(self, request, pk):
        cloud_server = get_object_or_404(
            CloudServer, pk=pk, organization=request.organization
        )
        if cloud_server.status == CloudServer.Status.DELETED:
            messages.error(request, "Droplet is already deleted.")
            return redirect("cloud:dashboard")

        from cloud.providers import get_provider
        try:
            provider = get_provider(cloud_server.cloud_account)
            if cloud_server.provider_server_id:
                success = provider.destroy_server(cloud_server.provider_server_id)
                if not success:
                    messages.error(request, "Failed to destroy droplet at provider.")
                    return redirect("cloud:dashboard")
        except Exception as exc:
            messages.error(request, f"Provider error: {exc}")
            return redirect("cloud:dashboard")

        cloud_server.status = CloudServer.Status.DELETED
        cloud_server.save(update_fields=["status", "updated_at"])

        # Mark infrastructure not ready
        if hasattr(cloud_server, "infrastructure"):
            cloud_server.infrastructure.is_ready = False
            cloud_server.infrastructure.save(update_fields=["is_ready"])

        log_audit(
            request.user,
            AuditLog.Action.DROPLET_DESTROY,
            request,
            f"Destroyed droplet '{cloud_server.name}'",
        )
        messages.success(request, f"Droplet '{cloud_server.name}' destroyed.")
        return redirect("cloud:dashboard")
