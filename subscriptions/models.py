from datetime import timedelta

from django.db import models
from django.utils import timezone


class Plan(models.Model):
    class PlanType(models.TextChoices):
        STARTER = "STARTER", "Starter"
        GROWTH = "GROWTH", "Growth"
        ENTERPRISE = "ENTERPRISE", "Enterprise"

    name = models.CharField(max_length=50, unique=True)
    plan_type = models.CharField(max_length=20, choices=PlanType.choices)
    price_monthly = models.DecimalField(max_digits=10, decimal_places=2)

    # Limits — null means unlimited (Enterprise)
    max_instances = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Maximum concurrent non-deleted instances. Null = unlimited.",
    )
    max_backups_per_month = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Maximum backup runs per calendar month. Null = unlimited.",
    )
    staging_enabled = models.BooleanField(default=False)
    version_upgrade_enabled = models.BooleanField(default=False)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["price_monthly"]

    def __str__(self):
        return f"{self.name} ({self.plan_type})"


class Subscription(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        PAST_DUE = "PAST_DUE", "Past Due"
        CANCELLED = "CANCELLED", "Cancelled"
        TRIAL = "TRIAL", "Trial"
        SUSPENDED = "SUSPENDED", "Suspended"

    GRACE_PERIOD_DAYS = 3

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="subscription",
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TRIAL)
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
    auto_renew = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.organization.name} — {self.plan.name} [{self.status}]"

    @property
    def is_in_grace_period(self):
        """PAST_DUE subscriptions get GRACE_PERIOD_DAYS days after period end."""
        if self.status != self.Status.PAST_DUE:
            return False
        grace_end = self.current_period_end + timedelta(days=self.GRACE_PERIOD_DAYS)
        return timezone.now() <= grace_end

    @property
    def is_serviceable(self):
        """
        True when provisioning actions should be permitted.
        ACTIVE         → always serviceable
        TRIAL          → serviceable until period end
        PAST_DUE       → serviceable within grace period
        CANCELLED/SUSPENDED → never serviceable
        """
        if self.status == self.Status.ACTIVE:
            return True
        if self.status == self.Status.TRIAL:
            return timezone.now() <= self.current_period_end
        if self.status == self.Status.PAST_DUE:
            return self.is_in_grace_period
        return False

    @property
    def days_until_renewal(self):
        delta = self.current_period_end - timezone.now()
        return max(0, delta.days)


class UsageRecord(models.Model):
    class UsageType(models.TextChoices):
        BACKUP = "BACKUP", "Backup"
        STAGING = "STAGING", "Staging"
        UPGRADE = "UPGRADE", "Upgrade"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="usage_records",
    )
    usage_type = models.CharField(max_length=20, choices=UsageType.choices)
    timestamp = models.DateTimeField(auto_now_add=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["organization", "usage_type", "timestamp"]),
        ]

    def __str__(self):
        return f"{self.organization.name} — {self.usage_type} @ {self.timestamp:%Y-%m-%d %H:%M}"
