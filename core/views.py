from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from datetime import timedelta
from django.views.generic import TemplateView, View

from audit.models import AuditLog
from core.utils import log_audit
from organizations.forms import InviteUserForm
from organizations.models import OrganizationMembership
from organizations.permissions import has_org_permission
from users.forms import ProfileUpdateForm

User = get_user_model()


class OrgRequiredMixin(LoginRequiredMixin):
    """Redirect to org selection if user has no current org."""
    def dispatch(self, request, *args, **kwargs):
        if not super().dispatch(request, *args, **kwargs).status_code == 200:
            return super().dispatch(request, *args, **kwargs)
        if request.user.is_authenticated and not request.organization:
            return redirect("organizations:select")
        return super().dispatch(request, *args, **kwargs)


class DashboardHomeView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/home.html"

    def dispatch(self, request, *args, **kwargs):
        resp = super().dispatch(request, *args, **kwargs)
        if request.user.is_authenticated and not request.organization:
            return redirect("organizations:select")
        return resp

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        ctx["total_members"] = OrganizationMembership.objects.filter(
            organization=org, is_active=True
        ).count() if org else 0
        ctx["recent_audit"] = (
            AuditLog.objects.filter(organization=org).select_related("user")[:10]
            if org else []
        )
        from deployments.models import Instance
        from subscriptions.models import Subscription

        ctx["active_deployments"] = (
            Instance.objects.filter(
                organization=org, status=Instance.Status.RUNNING
            ).count() if org else 0
        )
        ctx["subscription"] = None
        ctx["plan"] = None
        if org:
            try:
                sub = org.subscription
                ctx["subscription"] = sub
                ctx["plan"] = sub.plan
            except Subscription.DoesNotExist:
                pass
        return ctx


class ProfileView(LoginRequiredMixin, View):
    template_name = "dashboard/profile.html"

    def get(self, request):
        form = ProfileUpdateForm(instance=request.user)
        return render(request, self.template_name, {"form": form})

    def post(self, request):
        form = ProfileUpdateForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            log_audit(request.user, AuditLog.Action.PROFILE_UPDATE, request, "Profile updated")
            if request.headers.get("HX-Request"):
                return HttpResponse('<p class="text-green-600 font-medium">&#10003; Saved!</p>')
            messages.success(request, "Profile updated.")
            return redirect("core:profile")
        return render(request, self.template_name, {"form": form})


class UserManagementView(LoginRequiredMixin, TemplateView):
    """Org-scoped user/member management."""
    template_name = "dashboard/users.html"

    def dispatch(self, request, *args, **kwargs):
        resp = super().dispatch(request, *args, **kwargs)
        if not request.user.is_authenticated:
            return resp
        if not request.organization:
            return redirect("organizations:select")
        if request.org_role not in ("SUPER_ADMIN", "ADMIN"):
            messages.error(request, "Access denied.")
            return redirect("core:dashboard")
        return resp

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        q = self.request.GET.get("q", "")
        qs = OrganizationMembership.objects.select_related("user", "invited_by").filter(
            organization=org
        )
        if q:
            qs = qs.filter(user__email__icontains=q)
        ctx["members"] = qs.order_by("role", "joined_at")
        ctx["query"] = q
        ctx["invite_form"] = InviteUserForm(current_role=self.request.org_role)
        ctx["can_delete"] = self.request.org_role == "SUPER_ADMIN"
        ctx["can_change_role"] = self.request.org_role == "SUPER_ADMIN"
        return ctx

    def post(self, request, *args, **kwargs):
        """Handle invite submission via HTMX."""
        from organizations.views import _handle_invite
        org = request.organization
        member_list = OrganizationMembership.objects.select_related("user").filter(organization=org)
        return _handle_invite(request, org, member_list)


class AuditLogView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/audit.html"

    def dispatch(self, request, *args, **kwargs):
        resp = super().dispatch(request, *args, **kwargs)
        if not request.user.is_authenticated:
            return resp
        if not has_org_permission(request.user, request.organization, "view_logs") if request.organization else True:
            messages.error(request, "Access denied.")
            return redirect("core:dashboard")
        return resp

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        ctx["logs"] = (
            AuditLog.objects.filter(organization=org).select_related("user")[:100]
            if org else []
        )
        return ctx


class ConnectionsView(LoginRequiredMixin, TemplateView):
    """
    Unified connection hub (VCS + Cloud/PYOS connection management).
    """
    template_name = "dashboard/connections.html"

    def dispatch(self, request, *args, **kwargs):
        resp = super().dispatch(request, *args, **kwargs)
        if not request.user.is_authenticated:
            return resp
        if not request.organization:
            return redirect("organizations:select")
        if request.org_role not in ("SUPER_ADMIN", "ADMIN"):
            messages.error(request, "Access denied.")
            return redirect("core:dashboard")
        return resp

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from users.models import VCSAccount

        connected = set(
            VCSAccount.objects.filter(user=self.request.user, is_active=True)
            .values_list("provider", flat=True)
        )
        ctx["github_connected"] = "github" in connected
        ctx["gitlab_connected"] = "gitlab" in connected
        ctx["can_manage_cloud"] = getattr(self.request, "org_role", None) == "SUPER_ADMIN"
        return ctx


class VCSManagementView(LoginRequiredMixin, TemplateView):
    """
    Dashboard page for managing connected VCS (GitHub/GitLab) accounts.
    Any authenticated user can view; connect/disconnect allowed for SUPER_ADMIN + ADMIN.
    """
    template_name = "dashboard/vcs.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from users.models import VCSAccount

        ctx["vcs_accounts"] = VCSAccount.objects.filter(user=self.request.user)

        # Determine which providers are not yet connected (active)
        connected = set(
            VCSAccount.objects.filter(user=self.request.user, is_active=True)
            .values_list("provider", flat=True)
        )
        ctx["github_connected"] = "github" in connected
        ctx["gitlab_connected"] = "gitlab" in connected

        org_role = getattr(self.request, "org_role", None)
        ctx["can_manage"] = org_role in ("SUPER_ADMIN", "ADMIN")
        return ctx


class InstallationDocsView(LoginRequiredMixin, TemplateView):
    """In-app documentation for bare-metal Odoo installation via SSH."""
    template_name = "docs/installation.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        ctx["tabs"] = [
            {"id": "overview",      "label": "Overview"},
            {"id": "prerequisites", "label": "Prerequisites"},
            {"id": "manual",        "label": "Manual CLI"},
            {"id": "digitalocean",  "label": "DigitalOcean"},
            {"id": "aws",           "label": "AWS"},
            {"id": "config",        "label": "Env Config"},
        ]

        ctx["cli_options"] = [
            {"flag": "--ip",         "env": "DEPLOY_IP",           "default": "—",                    "desc": "Server IP address (required)"},
            {"flag": "--user",       "env": "DEPLOY_USER",         "default": "ubuntu",               "desc": "SSH username"},
            {"flag": "--key",        "env": "DEPLOY_KEY",          "default": "~/.ssh/id_rsa",        "desc": "Path to SSH private key"},
            {"flag": "--version",    "env": "DEPLOY_ODOO_VERSION", "default": "19",                   "desc": "Odoo major version: 17, 18, or 19"},
            {"flag": "--port",       "env": "DEPLOY_PORT",         "default": "8069",                 "desc": "Standalone Odoo HTTP port"},
            {"flag": "--domain",     "env": "DEPLOY_DOMAIN",       "default": "(empty)",              "desc": "FQDN — only used for standalone Nginx/SSL"},
            {"flag": "--email",      "env": "DEPLOY_ADMIN_EMAIL",  "default": "odoo@example.com",     "desc": "Admin e-mail for certbot / Let's Encrypt"},
            {"flag": "--standalone", "env": "DEPLOY_STANDALONE",   "default": "False",                "desc": "Also start a standalone Odoo service on the server"},
            {"flag": "--enterprise", "env": "DEPLOY_ENTERPRISE",   "default": "False",                "desc": "Set to install Odoo Enterprise edition"},
        ]

        ctx["env_vars"] = [
            # Ansible / playbook
            {"name": "ANSIBLE_ODOO_SERVER_PLAYBOOK",  "required": False, "default": "infra/ansible/setup_odoo_server_bare.yml", "desc": "Path to setup_odoo_server_bare.yml. If unset, DafeApp falls back to the repo-local playbook. The playbook picks the version-specific script from scripts/installscript/{ver}/odoo_install.sh."},
            {"name": "ANSIBLE_ODOO_INSTANCE_PLAYBOOK","required": False, "default": "—",                 "desc": "Path to the Ansible playbook for per-instance setup (nginx site, systemd service, SSL)."},
            {"name": "DEPLOY_STANDALONE",             "required": False, "default": "False",             "desc": "Set to True only if you want deploy_bare.sh to start a standalone Odoo service on the server."},
            {"name": "ODOO_ADMIN_EMAIL",              "required": False, "default": "odoo@example.com",  "desc": "E-mail passed to certbot. SSL is skipped when this is the placeholder value."},
            # Terraform
            {"name": "TERRAFORM_SERVER_MODULE_DIR",   "required": False, "default": "—",                 "desc": "Path to infra/terraform/odoo_server/. Required for managed-cloud (DO/AWS) provisioning."},
            {"name": "TERRAFORM_SSH_USER",            "required": False, "default": "ubuntu",            "desc": "SSH user used to validate the provisioned server after Terraform apply."},
            {"name": "TERRAFORM_SSH_KEY_PATH",        "required": False, "default": "~/.ssh/id_rsa",     "desc": "SSH key used for post-Terraform SSH validation."},
            # DigitalOcean
            {"name": "DIGITALOCEAN_TOKEN",            "required": False, "default": "—",                 "desc": "DigitalOcean Personal Access Token. Required when provider = DIGITALOCEAN."},
            # AWS
            {"name": "AWS_ACCESS_KEY_ID",             "required": False, "default": "—",                 "desc": "AWS IAM access key. Required when provider = AWS."},
            {"name": "AWS_SECRET_ACCESS_KEY",         "required": False, "default": "—",                 "desc": "AWS IAM secret key."},
            {"name": "AWS_DEFAULT_REGION",            "required": False, "default": "us-east-1",         "desc": "AWS region for Terraform and EC2."},
            # DNS
            {"name": "DNS_PROVIDER",                  "required": False, "default": "—",                 "desc": "DNS provider for automatic A-record creation: digitalocean or route53."},
            {"name": "DNS_ROOT_DOMAIN",               "required": False, "default": "—",                 "desc": "Root domain for DigitalOcean DNS (e.g. example.com)."},
            {"name": "DNS_CREATE_HOOK_CMD",           "required": False, "default": "—",                 "desc": "Path to scripts/create_dns_record.sh. Called with <fqdn> <ip> after provisioning."},
            {"name": "AWS_ROUTE53_ZONE_ID",           "required": False, "default": "—",                 "desc": "Route53 hosted zone ID for AWS DNS auto-creation."},
        ]

        return ctx
