from django.contrib import admin

from core.admin_filters import DeploymentJobAttentionFilter, InstanceAttentionFilter
from core.admin_mixins import (
    PLATFORM_OPERATIONS_ROLE,
    PLATFORM_OWNER_ROLE,
    PLATFORM_SUPPORT_ROLE,
    ReadOnlyAdminMixin,
    RoleControlledAdminMixin,
)
from deployments.models import (
    DeploymentJob,
    GitHubWebhookEvent,
    GitRepositoryCredential,
    Infrastructure,
    Instance,
    EnterpriseSource,
    OdooInstance,
    OdooInstanceHistory,
    OdooInstanceGitRepo,
    OdooServer,
    OdooServerHistory,
    ServerSSHKey,
    StagingEnvironment,
    TerraformRun,
)


@admin.register(Instance)
class InstanceAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

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
class TerraformRunAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    list_display = ["id", "instance", "status", "started_at", "finished_at", "created_at"]
    list_filter = ["status"]
    search_fields = ["instance__name", "instance__organization__name"]


@admin.register(OdooServer)
class OdooServerAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

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
    actions = ["reprovision_servers"]

    @admin.action(description="Queue reprovision for selected Odoo servers")
    def reprovision_servers(self, request, queryset):
        from deployments.tasks import provision_odoo_server

        for server in queryset:
            provision_odoo_server.delay(server.pk)
        self.message_user(request, f"Queued reprovision for {queryset.count()} Odoo server(s).")


@admin.register(OdooInstance)
class OdooInstanceAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

    list_display = [
        "name",
        "organization",
        "server",
        "db_name",
        "domain",
        "enterprise_enabled",
        "enterprise_status",
        "status",
        "created_at",
    ]
    list_filter = [InstanceAttentionFilter, "status", "ssl_enabled", "enterprise_enabled", "enterprise_status"]
    search_fields = ["name", "db_name", "domain", "organization__name", "server__name"]


@admin.register(EnterpriseSource)
class EnterpriseSourceAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}

    list_display = [
        "package_name",
        "odoo_version",
        "status",
        "is_active",
        "uploaded_by",
        "created_at",
    ]
    list_filter = ["odoo_version", "status", "is_active"]
    search_fields = ["package_name", "archive_filename", "addons_source_path"]


@admin.register(OdooInstanceGitRepo)
class OdooInstanceGitRepoAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

    list_display = [
        "repo_name",
        "instance",
        "credential",
        "branch",
        "auth_type",
        "auto_update",
        "install_requirements_on_update",
        "auto_upgrade_modules_on_update",
        "status",
        "last_pulled_at",
        "created_at",
    ]
    list_filter = ["auth_type", "auto_update", "install_requirements_on_update", "auto_upgrade_modules_on_update", "status", "is_enabled"]
    search_fields = ["repo_name", "git_url", "instance__name", "instance__organization__name"]


@admin.register(GitRepositoryCredential)
class GitRepositoryCredentialAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

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


@admin.register(GitHubWebhookEvent)
class GitHubWebhookEventAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    list_display = ["repository", "branch", "status", "pusher_name", "head_commit_sha", "received_at"]
    list_filter = ["status", "received_at"]
    search_fields = ["repository", "branch", "pusher_name"]
    readonly_fields = [
        "repository", "branch", "head_commit_sha", "head_commit_message",
        "pusher_name", "status", "ignore_reason", "matched_repo_ids",
        "queued_repo_ids", "received_at",
    ]


@admin.register(DeploymentJob)
class DeploymentJobAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

    list_display = [
        "id",
        "organization",
        "job_type",
        "status",
        "odoo_server",
        "odoo_instance",
        "created_by",
        "created_at",
        "finished_at",
    ]
    list_filter = [DeploymentJobAttentionFilter, "status", "job_type", "organization"]
    search_fields = ["organization__name", "odoo_server__name", "odoo_instance__name", "created_by__email"]
    readonly_fields = ["created_at", "updated_at", "started_at", "finished_at"]
    actions = ["cancel_jobs"]

    @admin.action(description="Cancel selected queued/running jobs")
    def cancel_jobs(self, request, queryset):
        from celery import current_app
        from django.utils import timezone

        cancelled = 0
        for job in queryset.filter(status__in=["QUEUED", "RUNNING"]):
            if job.celery_task_id:
                try:
                    current_app.control.revoke(job.celery_task_id, terminate=True, signal="SIGTERM")
                except Exception:
                    pass
            job.status = "CANCELLED"
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "finished_at", "updated_at"])
            cancelled += 1
        self.message_user(request, f"Cancelled {cancelled} deployment job(s).")


@admin.register(Infrastructure)
class InfrastructureAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

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


@admin.register(ServerSSHKey)
class ServerSSHKeyAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    list_display = ["label", "server", "added_by", "deployed", "created_at"]
    list_filter = ["deployed"]
    search_fields = ["label", "server__name", "server__organization__name"]


@admin.register(OdooServerHistory)
class OdooServerHistoryAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    list_display = ["server", "odoo_version", "ip_address", "status", "deployed_by", "deployed_at"]
    list_filter = ["status", "odoo_version"]
    search_fields = ["server__name", "server__organization__name", "ip_address", "note"]


@admin.register(OdooInstanceHistory)
class OdooInstanceHistoryAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    list_display = ["instance", "db_name", "domain", "status", "deployed_by", "deployed_at"]
    list_filter = ["status", "ssl_enabled", "odoo_version"]
    search_fields = ["instance__name", "instance__organization__name", "db_name", "domain", "note"]


@admin.register(StagingEnvironment)
class StagingEnvironmentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_OPERATIONS_ROLE, PLATFORM_SUPPORT_ROLE}
    list_display = [
        "staging_instance",
        "source_instance",
        "branch",
        "auto_delete_enabled",
        "ttl_days",
        "created_by",
        "created_at",
    ]
    list_filter = ["auto_delete_enabled", "ttl_days"]
    search_fields = [
        "staging_instance__name",
        "source_instance__name",
        "staging_instance__organization__name",
        "branch",
    ]
