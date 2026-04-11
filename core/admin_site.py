from datetime import timedelta

from django.contrib.admin import AdminSite
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.admin_mixins import (
    PLATFORM_FINANCE_ROLE,
    PLATFORM_OPERATIONS_ROLE,
    PLATFORM_OWNER_ROLE,
    PLATFORM_SUPPORT_ROLE,
    effective_platform_role,
)

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
        if not (user.is_staff or getattr(user, "is_platform_admin", False) or getattr(user, "platform_role", "")):
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
        return user.is_active and (
            user.is_staff
            or getattr(user, "is_platform_admin", False)
            or bool(getattr(user, "platform_role", ""))
        )

    def index(self, request, extra_context=None):
        context = self._build_dashboard_context(request)
        if extra_context:
            context.update(extra_context)
        return super().index(request, extra_context=context)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path("orgs/<int:org_id>/view-as/", self.admin_view(self.view_as_organization), name="view-as-organization"),
            path("orgs/stop-view-as/", self.admin_view(self.stop_view_as_organization), name="stop-view-as-organization"),
            path("ops/run-connectivity-sweep/", self.admin_view(self.run_connectivity_sweep), name="run-connectivity-sweep"),
            path("ops/run-instance-health-check/", self.admin_view(self.run_instance_health_check), name="run-instance-health-check"),
        ]
        return custom_urls + urls

    def view_as_organization(self, request, org_id):
        from organizations.models import Organization

        if not (request.user.is_platform_admin or request.user.is_superuser):
            raise PermissionDenied
        org = Organization.objects.filter(pk=org_id).first()
        if org:
            request.session["platform_view_as_org_id"] = org.pk
            request.session["current_org_id"] = org.pk
            self.message_user(request, f"You are now viewing the platform as {org.name}.")
        return HttpResponseRedirect(request.GET.get("next") or "/dashboard/")

    def stop_view_as_organization(self, request):
        request.session.pop("platform_view_as_org_id", None)
        self.message_user(request, "Stopped organization view mode.")
        return HttpResponseRedirect(request.GET.get("next") or reverse(f"{self.name}:index"))

    def run_connectivity_sweep(self, request):
        from deployments.tasks import check_server_connectivity

        check_server_connectivity.delay()
        self.message_user(request, "Connectivity sweep queued.")
        return HttpResponseRedirect(request.GET.get("next") or reverse(f"{self.name}:index"))

    def run_instance_health_check(self, request):
        from deployments.tasks import check_instance_health

        check_instance_health.delay()
        self.message_user(request, "Instance health check queued.")
        return HttpResponseRedirect(request.GET.get("next") or reverse(f"{self.name}:index"))

    def _build_dashboard_context(self, request):
        from audit.models import AuditLog
        from cloud.models import CloudAccount, ExternalServer
        from deployments.models import DeploymentJob, OdooInstance, OdooServer
        from organizations.models import Organization
        from subscriptions.models import Subscription
        from users.models import User

        now = timezone.now()
        stale_cutoff = now - timedelta(days=7)
        soon_cutoff = now + timedelta(days=3)
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
        failed_jobs = DeploymentJob.objects.select_related("organization", "odoo_instance", "odoo_server").filter(
            status=DeploymentJob.Status.FAILED
        )[:8]
        unhealthy_instances = OdooInstance.objects.select_related("organization", "server").filter(
            Q(status=OdooInstance.Status.FAILED)
            | Q(status=OdooInstance.Status.RUNNING, is_reachable=False)
            | Q(domain_status=OdooInstance.DomainStatus.FAILED)
            | Q(ssl_status=OdooInstance.SSLStatus.FAILED)
        )[:8]
        stale_verifications = CloudAccount.objects.select_related("organization").filter(
            Q(is_verified=False) | Q(last_verified_at__lt=stale_cutoff)
        )[:8]
        subscription_risks = Subscription.objects.select_related("organization", "plan").filter(
            Q(status__in=[Subscription.Status.PAST_DUE, Subscription.Status.SUSPENDED, Subscription.Status.CANCELLED])
            | Q(status=Subscription.Status.TRIAL, current_period_end__lte=soon_cutoff)
        )[:8]

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

        workspace_sections = self._workspace_sections()
        role = effective_platform_role(request.user)
        role_sections = [section for section in workspace_sections if role in section["roles"]]
        org_shortcuts = [
            {
                "name": org.name,
                "slug": org.slug,
                "owner_email": org.owner.email,
                "members": org.active_members,
                "running_instances": org.running_instances,
                "org_url": self._change_url(Organization, org.pk),
                "subscription_url": f"{self._changelist_url(Subscription)}?organization__id__exact={org.pk}",
                "instances_url": f"{self._changelist_url(OdooInstance)}?organization__id__exact={org.pk}",
                "members_url": f"{self._changelist_url(User)}?memberships__organization__id__exact={org.pk}",
                "view_as_url": reverse(f"{self.name}:view-as-organization", args=[org.pk]),
            }
            for org in recent_organizations
        ]
        saved_presets = [
            {
                "label": "Failed deployment jobs",
                "meta": "Operations queue failures",
                "url": f"{self._changelist_url(DeploymentJob)}?ops_preset=failed",
            },
            {
                "label": "Unhealthy instances",
                "meta": "Failed or unreachable Odoo instances",
                "url": f"{self._changelist_url(OdooInstance)}?instance_preset=unhealthy",
            },
            {
                "label": "DNS / SSL issues",
                "meta": "Broken domain or certificate states",
                "url": f"{self._changelist_url(OdooInstance)}?instance_preset=dns_ssl",
            },
            {
                "label": "Stale cloud verifications",
                "meta": "Unverified or old account checks",
                "url": f"{self._changelist_url(CloudAccount)}?verification_state=stale",
            },
            {
                "label": "Subscription risk",
                "meta": "Past-due, suspended, or ending trial subscriptions",
                "url": f"{self._changelist_url(Subscription)}?billing_preset=at_risk",
            },
        ]
        current_view_org = None
        current_view_as_org_id = request.session.get("platform_view_as_org_id")
        if current_view_as_org_id:
            current_view_org = Organization.objects.filter(pk=current_view_as_org_id).first()

        return {
            "platform_stats": platform_stats,
            "platform_quick_links": quick_links,
            "platform_saved_presets": saved_presets,
            "recent_organizations": recent_organizations,
            "org_shortcuts": org_shortcuts,
            "recent_activity": recent_activity,
            "failed_jobs": failed_jobs,
            "unhealthy_instances": unhealthy_instances,
            "stale_verifications": stale_verifications,
            "subscription_risks": subscription_risks,
            "role_sections": role_sections,
            "effective_platform_role": role,
            "current_view_org": current_view_org,
            "stop_view_as_url": reverse(f"{self.name}:stop-view-as-organization"),
            "dashboard_url": "/dashboard/",
            "run_connectivity_sweep_url": reverse(f"{self.name}:run-connectivity-sweep"),
            "run_instance_health_check_url": reverse(f"{self.name}:run-instance-health-check"),
        }

    def _changelist_url(self, model):
        return reverse(f"{self.name}:{model._meta.app_label}_{model._meta.model_name}_changelist")

    def _change_url(self, model, obj_id):
        return reverse(f"{self.name}:{model._meta.app_label}_{model._meta.model_name}_change", args=[obj_id])

    def _quick_link(self, label, model, meta=""):
        return {
            "label": label,
            "meta": meta,
            "count": model._default_manager.count(),
            "url": self._changelist_url(model),
        }

    def _workspace_sections(self):
        from audit.models import AuditLog
        from cloud.models import CloudAccount, ExternalServer
        from deployments.models import DeploymentJob, OdooServer
        from organizations.models import Organization
        from subscriptions.models import Plan, Subscription, UsageRecord
        from users.models import User

        return [
            {
                "title": "Operations Workspace",
                "description": "Provisioning, connectivity, infrastructure, and deployment recovery.",
                "roles": {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE},
                "items": [
                    {"label": "Cloud Accounts", "url": self._changelist_url(CloudAccount)},
                    {"label": "External Servers", "url": self._changelist_url(ExternalServer)},
                    {"label": "Odoo Servers", "url": self._changelist_url(OdooServer)},
                    {"label": "Deployment Jobs", "url": self._changelist_url(DeploymentJob)},
                ],
            },
            {
                "title": "Support Workspace",
                "description": "Cross-org visibility for orgs, members, subscriptions, and audit trails.",
                "roles": {PLATFORM_OWNER_ROLE, PLATFORM_SUPPORT_ROLE},
                "items": [
                    {"label": "Organizations", "url": self._changelist_url(Organization)},
                    {"label": "Users", "url": self._changelist_url(User)},
                    {"label": "Subscriptions", "url": self._changelist_url(Subscription)},
                    {"label": "Audit Logs", "url": self._changelist_url(AuditLog)},
                ],
            },
            {
                "title": "Finance Workspace",
                "description": "Plan health, subscriptions, and usage reporting.",
                "roles": {PLATFORM_OWNER_ROLE, PLATFORM_FINANCE_ROLE},
                "items": [
                    {"label": "Plans", "url": self._changelist_url(Plan)},
                    {"label": "Subscriptions", "url": self._changelist_url(Subscription)},
                    {"label": "Usage Records", "url": self._changelist_url(UsageRecord)},
                ],
            },
        ]
