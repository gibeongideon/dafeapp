from django.contrib import admin

from deployments.models import (
    GitRepositoryCredential,
    Infrastructure,
    Instance,
    OdooInstance,
    OdooInstanceGitRepo,
    OdooServer,
    TerraformRun,
)


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
        "infrastructure",
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


@admin.register(OdooInstanceGitRepo)
class OdooInstanceGitRepoAdmin(admin.ModelAdmin):
    list_display = [
        "repo_name",
        "instance",
        "credential",
        "branch",
        "auth_type",
        "auto_update",
        "status",
        "last_pulled_at",
        "created_at",
    ]
    list_filter = ["auth_type", "auto_update", "status", "is_enabled"]
    search_fields = ["repo_name", "git_url", "instance__name", "instance__organization__name"]


@admin.register(GitRepositoryCredential)
class GitRepositoryCredentialAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "organization",
        "auth_type",
        "github_account",
        "git_username",
        "last_used_at",
        "created_at",
    ]
    list_filter = ["auth_type"]
    search_fields = ["name", "organization__name", "git_username", "github_account__username"]


@admin.register(Infrastructure)
class InfrastructureAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "organization",
        "infra_type",
        "external_server",
        "cloud_account",
        "is_connected",
        "created_at",
    ]
    list_filter = ["infra_type", "is_connected"]
    search_fields = ["name", "organization__name"]
