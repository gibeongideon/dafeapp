from django.contrib import admin
from django.utils.html import format_html

from cloud.models import CloudAccount, CloudServer, ExternalServer, Infrastructure


@admin.register(ExternalServer)
class ExternalServerAdmin(admin.ModelAdmin):
    list_display = [
        "name", "host", "port", "username", "auth_type",
        "is_verified", "is_prepared", "preparation_status", "created_at",
    ]
    list_filter = ["is_verified", "is_prepared", "preparation_status", "auth_type"]
    search_fields = ["name", "host", "organization__name"]
    readonly_fields = [
        "encrypted_private_key_display",
        "encrypted_password_display",
        "is_verified", "is_prepared", "verification_error",
        "preparation_status", "preparation_log", "last_verified_at",
        "created_at", "updated_at",
    ]
    exclude = ["encrypted_private_key", "encrypted_password"]

    def encrypted_private_key_display(self, obj):
        return "[encrypted]" if obj.encrypted_private_key else "—"
    encrypted_private_key_display.short_description = "Private Key"

    def encrypted_password_display(self, obj):
        return "[encrypted]" if obj.encrypted_password else "—"
    encrypted_password_display.short_description = "Password"


@admin.register(CloudAccount)
class CloudAccountAdmin(admin.ModelAdmin):
    list_display = [
        "name", "provider", "organization", "verified_badge", "last_verified_at", "created_at",
    ]
    list_filter = ["provider", "is_verified"]
    search_fields = ["name", "organization__name"]
    readonly_fields = [
        "encrypted_api_token_display",
        "encrypted_aws_access_key_id_display",
        "encrypted_aws_secret_access_key_display",
        "is_verified", "verification_error", "last_verified_at", "created_at",
    ]
    exclude = ["encrypted_api_token", "encrypted_aws_access_key_id", "encrypted_aws_secret_access_key"]

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


@admin.register(CloudServer)
class CloudServerAdmin(admin.ModelAdmin):
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
class InfrastructureAdmin(admin.ModelAdmin):
    list_display = ["name", "organization", "infra_type_badge", "is_ready", "created_at"]
    list_filter = ["infra_type", "is_ready"]
    search_fields = ["name", "organization__name"]

    def infra_type_badge(self, obj):
        label = obj.get_infra_type_display()
        if obj.infra_type == "PYOS":
            return format_html('<span style="color:#555;">{}</span>', label)
        return format_html('<span style="color:#0070f3;">{}</span>', label)
    infra_type_badge.short_description = "Type"
