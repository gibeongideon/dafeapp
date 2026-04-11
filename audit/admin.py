from django.contrib import admin

from core.admin_mixins import (
    PLATFORM_FINANCE_ROLE,
    PLATFORM_OPERATIONS_ROLE,
    PLATFORM_OWNER_ROLE,
    PLATFORM_SUPPORT_ROLE,
    ReadOnlyAdminMixin,
)
from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    view_roles = {
        PLATFORM_OWNER_ROLE,
        PLATFORM_OPERATIONS_ROLE,
        PLATFORM_SUPPORT_ROLE,
        PLATFORM_FINANCE_ROLE,
    }
    list_display = ("timestamp", "organization", "user", "action", "ip_address", "description")
    list_filter = ("action", "organization", "timestamp")
    search_fields = ("user__email", "organization__name", "ip_address", "description")
    ordering = ("-timestamp",)
    readonly_fields = ("user", "action", "ip_address", "user_agent", "timestamp", "metadata", "description")
