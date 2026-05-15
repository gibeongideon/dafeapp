from django.contrib import admin
from django.utils.html import format_html

from core.admin_filters import SubscriptionRiskFilter
from core.admin_mixins import (
    PLATFORM_FINANCE_ROLE,
    PLATFORM_OWNER_ROLE,
    PLATFORM_SUPPORT_ROLE,
    ReadOnlyAdminMixin,
    RoleControlledAdminMixin,
)
from .models import Plan, PaystackPayment, Subscription, UsageRecord


@admin.register(Plan)
class PlanAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_FINANCE_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE, PLATFORM_FINANCE_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE, PLATFORM_FINANCE_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

    list_display = (
        "name", "plan_type", "price_monthly",
        "max_instances", "max_backups_per_month",
        "staging_enabled", "version_upgrade_enabled", "is_active",
    )
    list_filter = ("plan_type", "is_active", "staging_enabled", "version_upgrade_enabled")
    search_fields = ("name",)
    readonly_fields = ("created_at",)
    fieldsets = (
        (None, {"fields": ("name", "plan_type", "price_monthly", "is_active")}),
        ("Limits", {"fields": (
            "max_instances",
            "max_backups_per_month",
            "staging_enabled",
            "version_upgrade_enabled",
        )}),
        ("Metadata", {"fields": ("created_at",)}),
    )


@admin.register(Subscription)
class SubscriptionAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_FINANCE_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE, PLATFORM_FINANCE_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE, PLATFORM_FINANCE_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

    list_display = (
        "organization", "plan", "status_badge",
        "current_period_start", "current_period_end",
        "auto_renew", "created_at",
    )
    list_filter = (SubscriptionRiskFilter, "status", "plan", "auto_renew")
    search_fields = ("organization__name",)
    readonly_fields = ("created_at",)
    raw_id_fields = ("organization",)
    date_hierarchy = "created_at"

    @admin.display(description="Status")
    def status_badge(self, obj):
        colours = {
            "ACTIVE": "green",
            "TRIAL": "blue",
            "PAST_DUE": "orange",
            "SUSPENDED": "red",
            "CANCELLED": "gray",
        }
        colour = colours.get(obj.status, "gray")
        return format_html(
            '<span style="color:{}; font-weight:bold;">{}</span>',
            colour,
            obj.get_status_display(),
        )


@admin.register(PaystackPayment)
class PaystackPaymentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_FINANCE_ROLE, PLATFORM_SUPPORT_ROLE}
    list_display = (
        "reference", "organization", "plan", "amount", "currency",
        "status_badge", "payment_type", "paid_at", "created_at",
    )
    list_filter = ("status", "payment_type", "currency", "plan")
    search_fields = ("reference", "organization__name", "paystack_id")
    readonly_fields = ("reference", "paystack_id", "created_at", "paid_at", "metadata")
    raw_id_fields = ("organization",)
    date_hierarchy = "created_at"

    @admin.display(description="Status")
    def status_badge(self, obj):
        colours = {
            "SUCCESS": "green",
            "PENDING": "orange",
            "FAILED": "red",
            "ABANDONED": "gray",
        }
        colour = colours.get(obj.status, "gray")
        return format_html(
            '<span style="color:{}; font-weight:bold;">{}</span>',
            colour,
            obj.get_status_display(),
        )


@admin.register(UsageRecord)
class UsageRecordAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_FINANCE_ROLE, PLATFORM_SUPPORT_ROLE}
    list_display = ("organization", "usage_type", "timestamp", "notes")
    list_filter = ("usage_type",)
    search_fields = ("organization__name", "notes")
    readonly_fields = ("timestamp",)
    date_hierarchy = "timestamp"
    raw_id_fields = ("organization",)
