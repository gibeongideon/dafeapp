from rest_framework import serializers

from deployments.models import (
    DeploymentJob,
    Infrastructure,
    Instance,
    OdooInstance,
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
                return "SSH connection is being verified..."
            if ext.is_verified:
                return "SSH connection successful."
            if ext.verification_error:
                return ext.verification_error
            return "SSH connection has not been verified yet."
        if obj.status in (OdooServer.Status.PROVISIONING, OdooServer.Status.CONFIGURING) and obj.last_checked_at is None:
            return "SSH connection is being verified..."
        if obj.last_checked_at is None:
            return "SSH connection has not been checked yet."
        return "SSH connection successful." if obj.is_reachable else "SSH connection failed."

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

    def get_access_url(self, obj):
        return obj.access_url

    class Meta:
        model = OdooInstance
        fields = [
            "id",
            "name",
            "db_name",
            "domain",
            "http_port",
            "access_url",
            "requested_cpu_cores",
            "requested_ram_mb",
            "container_name",
            "systemd_service",
            "nginx_site",
            "ssl_enabled",
            "status",
            "provisioning_log",
            "server",
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
