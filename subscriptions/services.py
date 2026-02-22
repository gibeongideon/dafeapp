"""
SubscriptionEnforcer — the single source of truth for all subscription checks.

Usage in views / Celery tasks:
    enforcer = request.subscription_enforcer   # attached by SubscriptionMiddleware
    # — or —
    enforcer = SubscriptionEnforcer(request.organization)

    enforcer.ensure_active()                # raises SubscriptionError if not serviceable
    enforcer.check_instance_limit()         # raises SubscriptionLimitError if at cap
    enforcer.check_backup_limit()           # raises SubscriptionLimitError if at cap
    enforcer.check_staging_allowed()        # raises SubscriptionLimitError if disabled
    enforcer.check_upgrade_allowed()        # raises SubscriptionLimitError if disabled

    # After a successful action, record metered usage:
    enforcer.record_usage(UsageRecord.UsageType.BACKUP)
"""

from django.utils import timezone

from .exceptions import SubscriptionError, SubscriptionLimitError
from .models import Subscription, UsageRecord


class SubscriptionEnforcer:
    def __init__(self, organization):
        self.organization = organization
        try:
            self.subscription = organization.subscription
        except Subscription.DoesNotExist:
            self.subscription = None
        self.plan = self.subscription.plan if self.subscription else None

    # ── Active check ─────────────────────────────────────────────────────────

    def ensure_active(self):
        """Raise SubscriptionError if the subscription is not in a serviceable state."""
        if self.subscription is None:
            raise SubscriptionError(
                "No subscription found. Please contact support."
            )
        sub = self.subscription
        if sub.is_serviceable:
            return  # all good

        if sub.status == Subscription.Status.SUSPENDED:
            raise SubscriptionError(
                "Your subscription is suspended. Please contact support."
            )
        if sub.status == Subscription.Status.CANCELLED:
            raise SubscriptionError(
                "Your subscription has been cancelled. Please renew to continue."
            )
        if sub.status == Subscription.Status.PAST_DUE:
            raise SubscriptionError(
                "Your subscription is past due and the grace period has ended. "
                "Please update your payment method."
            )
        if sub.status == Subscription.Status.TRIAL:
            raise SubscriptionError(
                "Your trial has expired. Please upgrade to a paid plan."
            )
        raise SubscriptionError("Your subscription is not active.")

    # ── Limit checks ─────────────────────────────────────────────────────────

    def check_instance_limit(self):
        """Raise SubscriptionLimitError if the org is at or over its instance cap."""
        self.ensure_active()
        if self.plan.max_instances is None:
            return  # unlimited

        from deployments.models import Instance

        current_count = Instance.objects.filter(
            organization=self.organization
        ).exclude(status=Instance.Status.DELETED).count()

        if current_count >= self.plan.max_instances:
            raise SubscriptionLimitError(
                f"Instance limit reached ({current_count}/{self.plan.max_instances}). "
                f"Upgrade your plan to create more instances."
            )

    def check_backup_limit(self):
        """Raise SubscriptionLimitError if the org has hit its monthly backup cap."""
        self.ensure_active()
        if self.plan.max_backups_per_month is None:
            return  # unlimited

        now = timezone.now()
        current_count = UsageRecord.objects.filter(
            organization=self.organization,
            usage_type=UsageRecord.UsageType.BACKUP,
            timestamp__year=now.year,
            timestamp__month=now.month,
        ).count()

        if current_count >= self.plan.max_backups_per_month:
            raise SubscriptionLimitError(
                f"Monthly backup limit reached ({current_count}/{self.plan.max_backups_per_month}). "
                f"Upgrade your plan or wait until next month."
            )

    def check_staging_allowed(self):
        """Raise SubscriptionLimitError if staging is not enabled on the plan."""
        self.ensure_active()
        if not self.plan.staging_enabled:
            raise SubscriptionLimitError(
                "Staging environments are not available on the "
                f"{self.plan.name} plan. Upgrade to Growth or Enterprise."
            )

    def check_upgrade_allowed(self):
        """Raise SubscriptionLimitError if version upgrades are not enabled on the plan."""
        self.ensure_active()
        if not self.plan.version_upgrade_enabled:
            raise SubscriptionLimitError(
                "Automated version upgrades are not available on the "
                f"{self.plan.name} plan. Upgrade to Growth or Enterprise."
            )

    # ── Usage recording ───────────────────────────────────────────────────────

    def record_usage(self, usage_type, notes=""):
        """Record a metered usage event. Call after a successful action."""
        UsageRecord.objects.create(
            organization=self.organization,
            usage_type=usage_type,
            notes=notes,
        )

    # ── Read helpers (for templates / APIs) ───────────────────────────────────

    def current_instance_count(self):
        """Live count of non-deleted instances."""
        from deployments.models import Instance

        return Instance.objects.filter(
            organization=self.organization
        ).exclude(status=Instance.Status.DELETED).count()

    def current_backup_count_this_month(self):
        """Number of backup usage records in the current calendar month."""
        now = timezone.now()
        return UsageRecord.objects.filter(
            organization=self.organization,
            usage_type=UsageRecord.UsageType.BACKUP,
            timestamp__year=now.year,
            timestamp__month=now.month,
        ).count()

    @property
    def plan_limits(self):
        """Dict of limits for rendering in templates."""
        if not self.plan:
            return {}
        return {
            "max_instances": self.plan.max_instances,
            "max_backups_per_month": self.plan.max_backups_per_month,
            "staging_enabled": self.plan.staging_enabled,
            "version_upgrade_enabled": self.plan.version_upgrade_enabled,
            "current_instances": self.current_instance_count(),
            "current_backups_this_month": self.current_backup_count_this_month(),
        }
