from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.generic import TemplateView, View

from audit.models import AuditLog
from core.utils import log_audit
from users.forms import ProfileUpdateForm

User = get_user_model()


class DashboardHomeView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["total_users"] = User.objects.count()
        ctx["verified_users"] = User.objects.filter(is_email_verified=True).count()
        ctx["recent_audit"] = AuditLog.objects.select_related("user")[:10]
        ctx["active_deployments"] = 0
        ctx["total_tenants"] = 0
        ctx["active_subscriptions"] = 0
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
            messages.success(request, "Profile updated successfully.")
            return redirect("core:profile")
        return render(request, self.template_name, {"form": form})


class UserManagementView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/users.html"

    def get(self, request, *args, **kwargs):
        if not (request.user.is_staff or request.user.role == "admin"):
            messages.error(request, "Access denied.")
            return redirect("core:dashboard")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "")
        qs = User.objects.order_by("-date_joined")
        if q:
            qs = qs.filter(email__icontains=q)
        ctx["users"] = qs
        ctx["query"] = q
        return ctx


class AuditLogView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/audit.html"

    def get(self, request, *args, **kwargs):
        if not (request.user.is_staff or request.user.role in ("admin", "support")):
            messages.error(request, "Access denied.")
            return redirect("core:dashboard")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["logs"] = AuditLog.objects.select_related("user")[:100]
        return ctx
