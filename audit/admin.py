from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("user", "action", "ip_address", "description", "timestamp")
    list_filter = ("action", "timestamp")
    search_fields = ("user__email", "ip_address", "description")
    ordering = ("-timestamp",)
    readonly_fields = ("user", "action", "ip_address", "user_agent", "timestamp", "metadata", "description")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
