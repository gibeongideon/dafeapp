from datetime import timedelta

from django.contrib import admin
from django.utils import timezone


class VerificationStatusFilter(admin.SimpleListFilter):
    title = "verification state"
    parameter_name = "verification_state"

    def lookups(self, request, model_admin):
        return [
            ("unverified", "Unverified"),
            ("stale", "Stale"),
            ("healthy", "Healthy"),
        ]

    def queryset(self, request, queryset):
        value = self.value()
        cutoff = timezone.now() - timedelta(days=7)
        if value == "unverified":
            return queryset.filter(is_verified=False)
        if value == "stale":
            return queryset.filter(last_verified_at__lt=cutoff)
        if value == "healthy":
            return queryset.filter(is_verified=True, last_verified_at__gte=cutoff)
        return queryset


class DeploymentJobAttentionFilter(admin.SimpleListFilter):
    title = "ops preset"
    parameter_name = "ops_preset"

    def lookups(self, request, model_admin):
        return [
            ("failed", "Failed"),
            ("active", "Queued or running"),
            ("stale", "Running over 30 min"),
        ]

    def queryset(self, request, queryset):
        value = self.value()
        now = timezone.now()
        if value == "failed":
            return queryset.filter(status="FAILED")
        if value == "active":
            return queryset.filter(status__in=["QUEUED", "RUNNING"])
        if value == "stale":
            return queryset.filter(status="RUNNING", created_at__lt=now - timedelta(minutes=30))
        return queryset


class InstanceAttentionFilter(admin.SimpleListFilter):
    title = "instance preset"
    parameter_name = "instance_preset"

    def lookups(self, request, model_admin):
        return [
            ("failed", "Failed"),
            ("unhealthy", "Running but unreachable"),
            ("dns_ssl", "Domain or SSL issues"),
        ]

    def queryset(self, request, queryset):
        value = self.value()
        if value == "failed":
            return queryset.filter(status="FAILED")
        if value == "unhealthy":
            return queryset.filter(status="RUNNING", is_reachable=False)
        if value == "dns_ssl":
            return queryset.filter(domain_status="FAILED") | queryset.filter(ssl_status="FAILED")
        return queryset


class SubscriptionRiskFilter(admin.SimpleListFilter):
    title = "billing preset"
    parameter_name = "billing_preset"

    def lookups(self, request, model_admin):
        return [
            ("at_risk", "At risk"),
            ("trial_ending", "Trial ending soon"),
            ("healthy", "Healthy"),
        ]

    def queryset(self, request, queryset):
        value = self.value()
        soon = timezone.now() + timedelta(days=3)
        if value == "at_risk":
            return queryset.filter(status__in=["PAST_DUE", "SUSPENDED", "CANCELLED"])
        if value == "trial_ending":
            return queryset.filter(status="TRIAL", current_period_end__lte=soon)
        if value == "healthy":
            return queryset.filter(status="ACTIVE")
        return queryset
