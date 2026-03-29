from rest_framework import serializers

from dns.serializers import DomainAssignmentSerializer
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
    managed_dns_zone_name = serializers.SerializerMethodField()

    def _pyos_ext(self, obj):
        infra = getattr(obj, "infrastructure", None)
        if infra and infra.infra_type == Infrastructure.InfraType.PYOS:
            return getattr(infra, "external_server", None)
        return None

    def _reachability_snapshot(self, obj):
        ext = self._pyos_ext(obj)
        checked_at = obj.last_checked_at
        reachable = obj.is_reachable
        error = ""

        if ext:
            error = ext.verification_error or ""
            if checked_at is None and ext.last_checked_at:
                checked_at = ext.last_checked_at
                reachable = ext.is_reachable
            elif checked_at is None and ext.last_verified_at:
                checked_at = ext.last_verified_at
                reachable = ext.is_verified

        return reachable, checked_at, error

    def get_ssh_connection_status(self, obj):
        if obj.status == OdooServer.Status.ARCHIVED:
            return "unknown"
        reachable, checked_at, _ = self._reachability_snapshot(obj)
        if obj.status in (OdooServer.Status.PROVISIONING, OdooServer.Status.CONFIGURING) and checked_at is None:
            return "checking"
        if checked_at is None:
            return "unknown"
        return "connected" if reachable else "disconnected"

    def get_ssh_connection_message(self, obj):
        if obj.status == OdooServer.Status.ARCHIVED:
            return "Server is archived."
        reachable, checked_at, error = self._reachability_snapshot(obj)
        if obj.status in (OdooServer.Status.PROVISIONING, OdooServer.Status.CONFIGURING) and checked_at is None:
            return "Reachability is being verified..."
        if checked_at is None:
            return "Reachability has not been checked yet."
        if reachable:
            return "Reachability successful."
        return error or "Reachability failed."

    def get_ssh_last_checked_at(self, obj):
        _, checked_at, _ = self._reachability_snapshot(obj)
        return checked_at

    def get_instance_count(self, obj):
        return obj.instances.exclude(status=OdooInstance.Status.DELETED).count()

    def get_managed_dns_zone_name(self, obj):
        return obj.managed_dns_zone.name if obj.managed_dns_zone_id else ""

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
            "managed_dns_enabled",
            "managed_dns_zone",
            "managed_dns_zone_name",
            "domain_routing_enabled",
            "tls_mode",
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
    direct_access_url = serializers.SerializerMethodField()
    domain_access_url = serializers.SerializerMethodField()
    preferred_access_url = serializers.SerializerMethodField()
    owner_name = serializers.SerializerMethodField()
    storage_path = serializers.SerializerMethodField()
    domain_assignment = serializers.SerializerMethodField()

    def get_access_url(self, obj):
        return obj.access_url

    def get_direct_access_url(self, obj):
        return obj.direct_access_url

    def get_domain_access_url(self, obj):
        return obj.domain_access_url

    def get_preferred_access_url(self, obj):
        return obj.preferred_access_url

    def get_owner_name(self, obj):
        if not obj.created_by:
            return ""
        full_name = obj.created_by.get_full_name().strip()
        return full_name or obj.created_by.get_username()

    def get_storage_path(self, obj):
        return obj.storage_path

    def get_domain_assignment(self, obj):
        assignment = getattr(obj, "active_domain_assignment", None)
        if assignment is None:
            return None
        return DomainAssignmentSerializer(assignment).data

    class Meta:
        model = OdooInstance
        fields = [
            "id",
            "name",
            "db_name",
            "domain",
            "http_port",
            "access_url",
            "direct_access_url",
            "domain_access_url",
            "preferred_access_url",
            "domain_status",
            "domain_last_checked_at",
            "domain_assignment",
            "owner_name",
            "storage_path",
            "requested_cpu_cores",
            "requested_ram_mb",
            "container_name",
            "systemd_service",
            "nginx_site",
            "ssl_enabled",
            "ssl_status",
            "ssl_error",
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
