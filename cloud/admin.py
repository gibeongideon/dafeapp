from django.contrib import admin
from django.utils.html import format_html

from cloud.models import (
    CloudAccount,
    CloudServer,
    ExternalServer,
    Infrastructure,
    PyOSSSHSettings,
    SystemSSHKey,
)
from core.admin_filters import VerificationStatusFilter
from core.admin_mixins import (
    PLATFORM_FINANCE_ROLE,
    PLATFORM_OPERATIONS_ROLE,
    PLATFORM_OWNER_ROLE,
    PLATFORM_SUPPORT_ROLE,
    ReadOnlyAdminMixin,
    RoleControlledAdminMixin,
)


@admin.register(ExternalServer)
class ExternalServerAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

    list_display = [
        "name", "organization", "host", "port", "username", "auth_type",
        "is_verified", "is_prepared", "preparation_status", "created_at",
    ]
    list_filter = [VerificationStatusFilter, "is_prepared", "preparation_status", "auth_type", "organization"]
    search_fields = ["name", "host", "organization__name"]
    readonly_fields = [
        "encrypted_password_display",
        "is_verified", "is_prepared", "verification_error",
        "preparation_status", "preparation_log", "last_verified_at",
        "created_at", "updated_at",
    ]
    exclude = ["encrypted_password"]
    actions = ["reverify_servers", "prepare_servers"]

    def encrypted_password_display(self, obj):
        return "[encrypted]" if obj.encrypted_password else "—"
    encrypted_password_display.short_description = "Password"

    @admin.action(description="Re-verify selected external servers")
    def reverify_servers(self, request, queryset):
        from cloud.tasks import validate_external_server

        for server in queryset:
            validate_external_server.delay(server.pk)
        self.message_user(request, f"Queued verification for {queryset.count()} external server(s).")

    @admin.action(description="Prepare selected external servers")
    def prepare_servers(self, request, queryset):
        from cloud.tasks import prepare_external_server

        for server in queryset:
            prepare_external_server.delay(server.pk)
        self.message_user(request, f"Queued preparation for {queryset.count()} external server(s).")


@admin.register(CloudAccount)
class CloudAccountAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE, PLATFORM_FINANCE_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE, PLATFORM_FINANCE_ROLE}

    list_display = [
        "name", "provider", "organization", "is_platform_badge", "verified_badge", "last_verified_at", "created_at",
    ]
    list_filter = ["provider", "is_platform", VerificationStatusFilter, "organization"]
    search_fields = ["name", "organization__name"]
    readonly_fields = [
        "encrypted_api_token_display",
        "encrypted_aws_access_key_id_display",
        "encrypted_aws_secret_access_key_display",
        "is_verified", "verification_error", "last_verified_at", "created_at",
    ]
    exclude = ["encrypted_api_token", "encrypted_aws_access_key_id", "encrypted_aws_secret_access_key"]
    actions = ["reverify_accounts"]

    def encrypted_api_token_display(self, obj):
        return "[encrypted]" if obj.encrypted_api_token else "—"
    encrypted_api_token_display.short_description = "API Token"

    def encrypted_aws_access_key_id_display(self, obj):
        return "[encrypted]" if obj.encrypted_aws_access_key_id else "—"
    encrypted_aws_access_key_id_display.short_description = "AWS Access Key ID"

    def encrypted_aws_secret_access_key_display(self, obj):
        return "[encrypted]" if obj.encrypted_aws_secret_access_key else "—"
    encrypted_aws_secret_access_key_display.short_description = "AWS Secret Access Key"

    def verified_badge(self, obj):
        if obj.is_verified:
            return format_html('<span style="color:green;font-weight:bold;">✓ Verified</span>')
        return format_html('<span style="color:gray;">✗ Unverified</span>')
    verified_badge.short_description = "Status"

    def is_platform_badge(self, obj):
        if obj.is_platform:
            return format_html('<span style="color:#7c3aed;font-weight:bold;">★ Platform</span>')
        return "—"
    is_platform_badge.short_description = "Platform"

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            readonly.append("is_platform")
        return readonly

    @admin.action(description="Re-verify selected cloud accounts")
    def reverify_accounts(self, request, queryset):
        from cloud.tasks import validate_cloud_account

        for account in queryset:
            validate_cloud_account.delay(account.pk)
        self.message_user(request, f"Queued verification for {queryset.count()} cloud account(s).")


@admin.register(CloudServer)
class CloudServerAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

    list_display = [
        "name", "organization", "cloud_account", "region", "size",
        "ip_address", "status_badge", "created_at",
    ]
    list_filter = ["status", "region"]
    search_fields = ["name", "organization__name", "provider_server_id"]
    readonly_fields = ["provider_server_id", "ip_address", "status", "created_at", "updated_at"]

    def status_badge(self, obj):
        colors = {
            "PENDING": "gray",
            "PROVISIONING": "orange",
            "RUNNING": "green",
            "STOPPED": "gray",
            "FAILED": "red",
            "DELETED": "lightgray",
        }
        color = colors.get(obj.status, "gray")
        return format_html(
            '<span style="color:{};font-weight:bold;">{}</span>', color, obj.get_status_display()
        )
    status_badge.short_description = "Status"


@admin.register(Infrastructure)
class InfrastructureAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

    list_display = ["name", "organization", "infra_type_badge", "is_ready", "created_at"]
    list_filter = ["infra_type", "is_ready"]
    search_fields = ["name", "organization__name"]

    def infra_type_badge(self, obj):
        label = obj.get_infra_type_display()
        if obj.infra_type == "PYOS":
            return format_html('<span style="color:#555;">{}</span>', label)
        return format_html('<span style="color:#0070f3;">{}</span>', label)
    infra_type_badge.short_description = "Type"


@admin.register(PyOSSSHSettings)
class PyOSSSHSettingsAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    list_display = ["default_ssh_key_path", "updated_at"]


@admin.register(SystemSSHKey)
class SystemSSHKeyAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    list_display = ["public_key_preview", "created_at"]
    readonly_fields = ["public_key", "encrypted_private_key", "created_at"]

    @admin.display(description="Public Key")
    def public_key_preview(self, obj):
        return (obj.public_key or "")[:50] + "..."
