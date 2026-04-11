import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.http import JsonResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import TemplateView, View

from audit.models import AuditLog
from cloud.forms import (
    CloudAccountForm,
    ExternalServerForm,
    ProvisionDropletForm,
    PyOSSSHSettingsForm,
)
from cloud.models import (
    CloudAccount,
    CloudServer,
    ExternalServer,
    Infrastructure,
    PyOSSSHSettings,
    SystemSSHKey,
)
from cloud.providers import get_provider
from core.utils import log_audit

logger = logging.getLogger(__name__)

DO_OAUTH_AUTHORIZE_URL = "https://cloud.digitalocean.com/v1/oauth/authorize"
DO_OAUTH_TOKEN_URL = "https://cloud.digitalocean.com/v1/oauth/token"
DO_OAUTH_STATE_SESSION_KEY = "do_oauth_state"


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
        ctx["pyos_ssh_settings"] = PyOSSSHSettings.get_or_create_settings()
        ctx["digitalocean_oauth_enabled"] = _digitalocean_oauth_enabled()
        return ctx


# ── External (PYOS) servers ─────────────────────────────────────────────────

class AddExternalServerView(CloudSuperAdminMixin, View):
    template_name = "cloud/add_server.html"

    def _ctx(self, form):
        key_obj = SystemSSHKey.get_or_create_keypair()
        settings_obj = PyOSSSHSettings.get_or_create_settings()
        return {
            "form": form,
            "dafeapp_public_key": key_obj.public_key,
            "pyos_default_ssh_key_path": settings_obj.default_ssh_key_path,
        }

    def get(self, request):
        form = ExternalServerForm()
        return render(request, self.template_name, self._ctx(form))

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

        return render(request, self.template_name, self._ctx(form))


class PyOSSSHSettingsView(CloudSuperAdminMixin, View):
    template_name = "cloud/ssh_settings.html"

    def get(self, request):
        settings_obj = PyOSSSHSettings.get_or_create_settings()
        form = PyOSSSHSettingsForm(instance=settings_obj)
        return render(request, self.template_name, {"form": form, "settings": settings_obj})

    def post(self, request):
        settings_obj = PyOSSSHSettings.get_or_create_settings()
        form = PyOSSSHSettingsForm(request.POST, instance=settings_obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Default SSH key path saved.")
            return redirect("cloud:ssh-settings")
        return render(request, self.template_name, {"form": form, "settings": settings_obj})


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
        provider = request.GET.get("provider")
        initial = {}
        if provider in {
            CloudAccount.Provider.DIGITALOCEAN,
            CloudAccount.Provider.AWS,
        }:
            initial["provider"] = provider
        return render(
            request,
            self.template_name,
            {
                "form": CloudAccountForm(initial=initial),
                "digitalocean_oauth_enabled": _digitalocean_oauth_enabled(),
            },
        )

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

            # Dispatch after DB commit so the task always finds the row.
            from cloud.tasks import validate_cloud_account
            from django.db import transaction
            transaction.on_commit(lambda: _dispatch(validate_cloud_account, account.pk))

            return redirect("cloud:dashboard")

        return render(
            request,
            self.template_name,
            {"form": form, "digitalocean_oauth_enabled": _digitalocean_oauth_enabled()},
        )


def _digitalocean_oauth_enabled() -> bool:
    return bool(
        getattr(settings, "DIGITALOCEAN_CLIENT_ID", "").strip()
        and getattr(settings, "DIGITALOCEAN_CLIENT_SECRET", "").strip()
    )


def _digitalocean_redirect_uri(request) -> str:
    return request.build_absolute_uri(reverse("cloud:digitalocean-oauth-callback"))


class DigitalOceanOAuthStartView(CloudSuperAdminMixin, View):
    def get(self, request):
        if not _digitalocean_oauth_enabled():
            messages.error(
                request,
                "DigitalOcean OAuth is not configured yet. Set DIGITALOCEAN_CLIENT_ID and DIGITALOCEAN_CLIENT_SECRET first.",
            )
            return redirect("cloud:add-account")

        state = secrets.token_urlsafe(24)
        request.session[DO_OAUTH_STATE_SESSION_KEY] = state
        params = {
            "client_id": settings.DIGITALOCEAN_CLIENT_ID,
            "redirect_uri": _digitalocean_redirect_uri(request),
            "response_type": "code",
            "scope": "read write",
            "state": state,
        }
        return redirect(f"{DO_OAUTH_AUTHORIZE_URL}?{urlencode(params)}")


class DigitalOceanOAuthCallbackView(CloudSuperAdminMixin, View):
    def get(self, request):
        expected_state = request.session.pop(DO_OAUTH_STATE_SESSION_KEY, "")
        returned_state = (request.GET.get("state") or "").strip()
        if not expected_state or not returned_state or expected_state != returned_state:
            messages.error(request, "DigitalOcean OAuth state check failed. Please try again.")
            return redirect("cloud:add-account")

        if request.GET.get("error"):
            error = request.GET.get("error_description") or request.GET.get("error") or "Authorization was denied."
            messages.error(request, f"DigitalOcean OAuth failed: {error}")
            return redirect("cloud:add-account")

        code = (request.GET.get("code") or "").strip()
        if not code:
            messages.error(request, "DigitalOcean did not return an authorization code.")
            return redirect("cloud:add-account")

        try:
            response = requests.post(
                DO_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": settings.DIGITALOCEAN_CLIENT_ID,
                    "client_secret": settings.DIGITALOCEAN_CLIENT_SECRET,
                    "redirect_uri": _digitalocean_redirect_uri(request),
                },
                timeout=20,
            )
            payload = response.json()
        except requests.RequestException as exc:
            messages.error(request, f"Could not complete DigitalOcean OAuth: {exc}")
            return redirect("cloud:add-account")
        except ValueError:
            payload = {}

        if not response.ok:
            messages.error(
                request,
                f"DigitalOcean OAuth token exchange failed: {payload.get('error_description') or payload.get('error') or response.status_code}",
            )
            return redirect("cloud:add-account")

        access_token = (payload.get("access_token") or "").strip()
        refresh_token = (payload.get("refresh_token") or "").strip()
        expires_in = payload.get("expires_in")
        if not access_token:
            messages.error(request, "DigitalOcean OAuth succeeded but no access token was returned.")
            return redirect("cloud:add-account")

        account = CloudAccount(
            organization=request.organization,
            provider=CloudAccount.Provider.DIGITALOCEAN,
            name=f"DigitalOcean OAuth · {timezone.now().strftime('%Y-%m-%d %H:%M')}",
            do_auth_method=CloudAccount.DOAuthMethod.OAUTH,
            encrypted_api_token="",
            encrypted_aws_access_key_id="",
            encrypted_aws_secret_access_key="",
            aws_default_region="",
        )
        account._raw_do_oauth_token = access_token
        if refresh_token:
            account._raw_do_oauth_refresh_token = refresh_token
        if expires_in:
            try:
                account.do_oauth_token_expiry = timezone.now() + timedelta(seconds=int(expires_in))
            except Exception:
                account.do_oauth_token_expiry = None
        account.save()

        log_audit(
            request.user,
            AuditLog.Action.CLOUD_ACCT_ADD,
            request,
            f"Added cloud account '{account.name}' ({account.get_provider_display()}) via OAuth",
        )
        messages.success(request, f"Account '{account.name}' connected. Verifying access…")

        from cloud.tasks import validate_cloud_account
        from django.db import transaction

        transaction.on_commit(lambda: _dispatch(validate_cloud_account, account.pk))
        return redirect("cloud:dashboard")


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
            region = request.GET.get("region", "").strip()
            sizes = provider.list_sizes(region=region)
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


class DafeAppPublicKeyView(CloudSuperAdminMixin, View):
    """
    GET  → returns DafeApp's SSH public key as plain text.
    POST → regenerates the keypair (invalidates existing server access).
    """

    def get(self, request):
        key_obj = SystemSSHKey.get_or_create_keypair()
        return JsonResponse({"public_key": key_obj.public_key})

    def post(self, request):
        """Regenerate the keypair. Old public key must be removed from servers first."""
        SystemSSHKey.objects.all().delete()
        key_obj = SystemSSHKey.get_or_create_keypair()
        return JsonResponse({"public_key": key_obj.public_key, "regenerated": True})
