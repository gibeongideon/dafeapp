from rest_framework import serializers

from deployments.models import (
    DeploymentJob,
    GitRepositoryCredential,
    Infrastructure,
    Instance,
    OdooInstance,
    OdooInstanceGitRepo,
    OdooInstanceHistory,
    OdooServer,
    OdooServerHistory,
    TerraformRun,
)


class InstanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Instance
        fields = [
            "id",
            "name",
            "status",
            "region",
            "size",
            "ip_address",
            "provisioning_log",
            "created_at",
            "updated_at",
        ]


class TerraformRunSerializer(serializers.ModelSerializer):
    instance = InstanceSerializer(read_only=True)

    class Meta:
        model = TerraformRun
        fields = [
            "id",
            "status",
            "command",
            "output_log",
            "error_log",
            "state_file_path",
            "metadata",
            "started_at",
            "finished_at",
            "created_at",
            "instance",
        ]


class OdooServerSerializer(serializers.ModelSerializer):
    ssh_connection_status = serializers.SerializerMethodField()
    ssh_connection_message = serializers.SerializerMethodField()
    ssh_last_checked_at = serializers.SerializerMethodField()
    instance_count = serializers.SerializerMethodField()

    def _pyos_ext(self, obj):
        infra = getattr(obj, "infrastructure", None)
        if infra and infra.infra_type == Infrastructure.InfraType.PYOS:
            return getattr(infra, "external_server", None)
        return None

    def get_ssh_connection_status(self, obj):
        if obj.status == OdooServer.Status.ARCHIVED:
            return "unknown"
        ext = self._pyos_ext(obj)
        if ext:
            if obj.status in (OdooServer.Status.PROVISIONING, OdooServer.Status.CONFIGURING) and ext.last_verified_at is None:
                return "checking"
            if ext.last_verified_at is None and not ext.is_verified:
                return "unknown"
            return "connected" if ext.is_verified else "disconnected"
        if obj.status in (OdooServer.Status.PROVISIONING, OdooServer.Status.CONFIGURING) and obj.last_checked_at is None:
            return "checking"
        if obj.last_checked_at is None:
            return "unknown"
        return "connected" if obj.is_reachable else "disconnected"

    def get_ssh_connection_message(self, obj):
        if obj.status == OdooServer.Status.ARCHIVED:
            return "Server is archived."
        ext = self._pyos_ext(obj)
        if ext:
            if obj.status in (OdooServer.Status.PROVISIONING, OdooServer.Status.CONFIGURING) and ext.last_verified_at is None:
                return "Reachability is being verified..."
            if ext.is_verified:
                return "Reachability successful."
            if ext.verification_error:
                return ext.verification_error
            return "Reachability has not been verified yet."
        if obj.status in (OdooServer.Status.PROVISIONING, OdooServer.Status.CONFIGURING) and obj.last_checked_at is None:
            return "Reachability is being verified..."
        if obj.last_checked_at is None:
            return "Reachability has not been checked yet."
        return "Reachability successful." if obj.is_reachable else "Reachability failed."

    def get_ssh_last_checked_at(self, obj):
        ext = self._pyos_ext(obj)
        if ext and ext.last_verified_at:
            return ext.last_verified_at
        return obj.last_checked_at

    def get_instance_count(self, obj):
        return obj.instances.exclude(status=OdooInstance.Status.DELETED).count()

    class Meta:
        model = OdooServer
        fields = [
            "id",
            "name",
            "infrastructure",
            "odoo_version",
            "region",
            "size",
            "provider_server_id",
            "ip_address",
            "dns_domain",
            "firewall_configured",
            "status",
            "is_active",
            "max_instances",
            "instance_count",
            "capacity_cpu_cores",
            "capacity_ram_mb",
            "min_port",
            "max_port",
            "terraform_state_path",
            "provisioning_log",
            "installation_summary",
            "installation_summary_text",
            "deployment_mode",
            "is_reachable",
            "last_checked_at",
            "ssh_connection_status",
            "ssh_connection_message",
            "ssh_last_checked_at",
            "created_at",
            "updated_at",
        ]


class OdooInstanceSerializer(serializers.ModelSerializer):
    server = OdooServerSerializer(read_only=True)
    access_url = serializers.SerializerMethodField()
    owner_name = serializers.SerializerMethodField()
    storage_path = serializers.SerializerMethodField()

    def get_access_url(self, obj):
        return obj.access_url

    def get_owner_name(self, obj):
        if not obj.created_by:
            return ""
        full_name = obj.created_by.get_full_name().strip()
        return full_name or obj.created_by.get_username()

    def get_storage_path(self, obj):
        return obj.storage_path

    class Meta:
        model = OdooInstance
        fields = [
            "id",
            "name",
            "db_name",
            "domain",
            "http_port",
            "access_url",
            "owner_name",
            "storage_path",
            "requested_cpu_cores",
            "requested_ram_mb",
            "container_name",
            "systemd_service",
            "nginx_site",
            "ssl_enabled",
            "status",
            "provisioning_log",
            "installation_summary",
            "installation_summary_text",
            "server",
            "created_at",
            "updated_at",
        ]


class OdooInstanceGitRepoSerializer(serializers.ModelSerializer):
    instance_name = serializers.SerializerMethodField()
    credential_name = serializers.SerializerMethodField()

    def get_instance_name(self, obj):
        return obj.instance.name if obj.instance_id else ""

    def get_credential_name(self, obj):
        return obj.credential.name if obj.credential_id else ""

    class Meta:
        model = OdooInstanceGitRepo
        fields = [
            "id",
            "instance",
            "instance_name",
            "credential",
            "credential_name",
            "repo_name",
            "git_url",
            "branch",
            "auth_type",
            "local_path",
            "auto_update",
            "is_enabled",
            "display_order",
            "default_branch",
            "pinned_commit",
            "previous_commit",
            "last_remote_commit",
            "last_pulled_commit",
            "last_pulled_at",
            "last_sync_started_at",
            "last_sync_finished_at",
            "last_sync_log",
            "last_detected_modules",
            "status",
            "last_error",
            "created_at",
            "updated_at",
        ]


class GitRepositoryCredentialSerializer(serializers.ModelSerializer):
    github_account_username = serializers.SerializerMethodField()

    def get_github_account_username(self, obj):
        return obj.github_account.username if obj.github_account_id else ""

    class Meta:
        model = GitRepositoryCredential
        fields = [
            "id",
            "name",
            "auth_type",
            "github_account",
            "github_account_username",
            "git_username",
            "ssh_public_key",
            "notes",
            "last_used_at",
            "created_at",
            "updated_at",
        ]


class InfrastructureSerializer(serializers.ModelSerializer):
    class Meta:
        model = Infrastructure
        fields = [
            "id",
            "name",
            "infra_type",
            "external_server",
            "cloud_account",
            "is_connected",
            "validation_log",
            "created_at",
            "updated_at",
        ]


class DeploymentJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeploymentJob
        fields = [
            "id",
            "job_type",
            "status",
            "celery_task_id",
            "odoo_server",
            "odoo_instance",
            "log",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
        ]


class OdooServerHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = OdooServerHistory
        fields = [
            "id",
            "server",
            "odoo_version",
            "ip_address",
            "dns_domain",
            "region",
            "size",
            "status",
            "note",
            "deployed_by",
            "deployed_at",
        ]


class OdooInstanceHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = OdooInstanceHistory
        fields = [
            "id",
            "instance",
            "db_name",
            "domain",
            "http_port",
            "odoo_version",
            "server_ip",
            "systemd_service",
            "ssl_enabled",
            "status",
            "note",
            "deployed_by",
            "deployed_at",
        ]
