from django.contrib import admin

from dns.models import DomainAssignment, DnsProviderAccount, DnsRecord, DnsZone


@admin.register(DnsProviderAccount)
class DnsProviderAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "provider", "token_preview", "is_verified", "is_active", "last_verified_at")
    list_filter = ("provider", "is_verified", "is_active", "organization")
    readonly_fields = ("encrypted_api_token", "token_preview", "last_verified_at", "created_at", "updated_at")
    search_fields = ("name", "organization__name")

    def token_preview(self, obj):
        if not obj.encrypted_api_token:
            return "Not configured"
        return "Configured"


@admin.register(DnsZone)
class DnsZoneAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "provider_account", "default_proxied", "is_active", "last_synced_at")
    list_filter = ("organization", "provider_account__provider", "is_active", "default_proxied")
    search_fields = ("name", "organization__name", "provider_account__name")


@admin.register(DnsRecord)
class DnsRecordAdmin(admin.ModelAdmin):
    list_display = ("fqdn", "record_type", "value", "status", "proxied", "zone", "last_synced_at")
    list_filter = ("record_type", "status", "proxied", "zone")
    search_fields = ("hostname", "value", "zone__name")


@admin.register(DomainAssignment)
class DomainAssignmentAdmin(admin.ModelAdmin):
    list_display = ("domain", "instance", "zone", "status", "is_managed", "proxied", "last_synced_at")
    list_filter = ("status", "is_managed", "proxied", "zone")
    search_fields = ("domain", "hostname", "instance__name", "instance__db_name")
