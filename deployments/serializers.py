from rest_framework import serializers

from deployments.models import Instance, OdooInstance, OdooServer, TerraformRun


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
            "odoo_version",
            "region",
            "size",
            "provider_server_id",
            "ip_address",
            "dns_domain",
            "firewall_configured",
            "status",
            "terraform_state_path",
            "provisioning_log",
            "created_at",
            "updated_at",
        ]


class OdooInstanceSerializer(serializers.ModelSerializer):
    server = OdooServerSerializer(read_only=True)

    class Meta:
        model = OdooInstance
        fields = [
            "id",
            "name",
            "db_name",
            "domain",
            "http_port",
            "systemd_service",
            "nginx_site",
            "ssl_enabled",
            "status",
            "provisioning_log",
            "server",
            "created_at",
            "updated_at",
        ]
