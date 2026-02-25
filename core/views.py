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
