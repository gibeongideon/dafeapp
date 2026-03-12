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
            "max_instances",
            "capacity_cpu_cores",
            "capacity_ram_mb",
            "min_port",
            "max_port",
            "terraform_state_path",
            "provisioning_log",
            "is_reachable",
            "last_checked_at",
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
