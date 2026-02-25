from django.contrib import admin

from deployments.models import Instance, OdooInstance, OdooServer, TerraformRun


@admin.register(Instance)
class InstanceAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "organization",
        "cloud_account",
        "region",
        "size",
        "ip_address",
        "status",
        "created_at",
    ]
    list_filter = ["status", "region"]
    search_fields = ["name", "organization__name", "ip_address"]


@admin.register(TerraformRun)
class TerraformRunAdmin(admin.ModelAdmin):
    list_display = ["id", "instance", "status", "started_at", "finished_at", "created_at"]
    list_filter = ["status"]
    search_fields = ["instance__name", "instance__organization__name"]


@admin.register(OdooServer)
class OdooServerAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "organization",
        "odoo_version",
        "cloud_account",
        "region",
        "size",
        "ip_address",
        "status",
        "created_at",
    ]
    list_filter = ["odoo_version", "status", "region"]
    search_fields = ["name", "organization__name", "dns_domain", "provider_server_id"]


@admin.register(OdooInstance)
class OdooInstanceAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "organization",
        "server",
        "db_name",
        "domain",
        "status",
        "created_at",
    ]
    list_filter = ["status", "ssl_enabled"]
    search_fields = ["name", "db_name", "domain", "organization__name", "server__name"]
