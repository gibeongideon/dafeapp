from django.contrib.admin import AdminSite
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.urls import reverse
from django.utils.translation import gettext_lazy as _


class PlatformAdminAuthenticationForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": _(
            "Please enter the correct %(username)s and password for a staff "
            "or platform admin account. Note that both fields may be case-sensitive."
        ),
    }

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not (user.is_staff or getattr(user, "is_platform_admin", False)):
            raise ValidationError(
                self.error_messages["invalid_login"],
                code="invalid_login",
                params={"username": self.username_field.verbose_name},
            )


class PlatformAdminSite(AdminSite):
    site_header = "Dafe Platform Admin"
    site_title = "Dafe Platform Admin"
    index_title = "Platform Overview"
    index_template = "admin/platform_index.html"
    login_form = PlatformAdminAuthenticationForm
    site_url = "/dashboard/"

    def has_permission(self, request):
        user = request.user
        return user.is_active and (user.is_staff or getattr(user, "is_platform_admin", False))

    def index(self, request, extra_context=None):
        context = self._build_dashboard_context()
        if extra_context:
            context.update(extra_context)
        return super().index(request, extra_context=context)

    def _build_dashboard_context(self):
        from audit.models import AuditLog
        from cloud.models import CloudAccount, ExternalServer
        from deployments.models import DeploymentJob, OdooInstance, OdooServer
        from organizations.models import Organization
        from subscriptions.models import Subscription
        from users.models import User

        active_subscription_statuses = [
            Subscription.Status.ACTIVE,
            Subscription.Status.TRIAL,
        ]

        platform_stats = [
            {
                "label": "Organizations",
                "value": Organization.objects.count(),
                "meta": f"{Organization.objects.filter(is_active=True).count()} active",
                "url": self._changelist_url(Organization),
            },
            {
                "label": "Users",
                "value": User.objects.count(),
                "meta": f"{User.objects.filter(is_platform_admin=True, is_active=True).count()} platform admins",
                "url": self._changelist_url(User),
            },
            {
                "label": "Running Instances",
                "value": OdooInstance.objects.filter(status=OdooInstance.Status.RUNNING).count(),
                "meta": f"{OdooInstance.objects.filter(status=OdooInstance.Status.FAILED).count()} failed",
                "url": self._changelist_url(OdooInstance),
            },
            {
                "label": "Subscriptions",
                "value": Subscription.objects.filter(status__in=active_subscription_statuses).count(),
                "meta": f"{Subscription.objects.filter(status=Subscription.Status.PAST_DUE).count()} past due",
                "url": self._changelist_url(Subscription),
            },
            {
                "label": "Cloud Accounts",
                "value": CloudAccount.objects.filter(is_verified=True).count(),
                "meta": f"{CloudAccount.objects.filter(is_verified=False).count()} need verification",
                "url": self._changelist_url(CloudAccount),
            },
            {
                "label": "Deployment Jobs",
                "value": DeploymentJob.objects.filter(
                    status__in=[DeploymentJob.Status.QUEUED, DeploymentJob.Status.RUNNING]
                ).count(),
                "meta": f"{DeploymentJob.objects.filter(status=DeploymentJob.Status.FAILED).count()} failed",
                "url": self._changelist_url(DeploymentJob),
            },
        ]

        recent_organizations = (
            Organization.objects.select_related("owner")
            .annotate(
                active_members=Count(
                    "memberships",
                    filter=Q(memberships__is_active=True),
                    distinct=True,
                ),
                running_instances=Count(
                    "odoo_instances",
                    filter=Q(odoo_instances__status=OdooInstance.Status.RUNNING),
                    distinct=True,
                ),
            )
            .order_by("-created_at")[:8]
        )

        recent_activity = AuditLog.objects.select_related("user", "organization")[:12]

        quick_links = [
            self._quick_link("Organizations", Organization, meta="Tenants, owners, memberships"),
            self._quick_link("Users", User, meta="Platform admins, staff, auth state"),
            self._quick_link("Subscriptions", Subscription, meta="Billing status and plans"),
            self._quick_link("Cloud Accounts", CloudAccount, meta="Global credential inventory"),
            self._quick_link("External Servers", ExternalServer, meta="PYOS / SSH-managed hosts"),
            self._quick_link("Odoo Servers", OdooServer, meta="Provisioned hosts across orgs"),
            self._quick_link("Odoo Instances", OdooInstance, meta="Cross-org workload visibility"),
            self._quick_link("Deployment Jobs", DeploymentJob, meta="Queue, failures, retries"),
            self._quick_link("Audit Logs", AuditLog, meta="Latest platform events"),
        ]

        return {
            "platform_stats": platform_stats,
            "platform_quick_links": quick_links,
            "recent_organizations": recent_organizations,
            "recent_activity": recent_activity,
        }

    def _changelist_url(self, model):
        return reverse(f"{self.name}:{model._meta.app_label}_{model._meta.model_name}_changelist")

    def _quick_link(self, label, model, meta=""):
        return {
            "label": label,
            "meta": meta,
            "count": model._default_manager.count(),
            "url": self._changelist_url(model),
        }
