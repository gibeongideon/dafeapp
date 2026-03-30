from rest_framework import serializers

from dns.models import DomainAssignment, DnsProviderAccount, DnsRecord, DnsZone


class DnsProviderAccountSerializer(serializers.ModelSerializer):
    api_token = serializers.CharField(write_only=True, required=False, allow_blank=True)
    token_configured = serializers.SerializerMethodField()

    def get_token_configured(self, obj):
        return obj.token_configured

    def create(self, validated_data):
        api_token = validated_data.pop("api_token", None)
        account = DnsProviderAccount(**validated_data)
        if api_token is not None:
            account._raw_api_token = api_token
        account.save()
        return account

    def update(self, instance, validated_data):
        api_token = validated_data.pop("api_token", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if api_token is not None:
            instance._raw_api_token = api_token
        instance.save()
        return instance

    class Meta:
        model = DnsProviderAccount
        fields = [
            "id",
            "name",
            "provider",
            "api_token",
            "token_configured",
            "is_active",
            "is_verified",
            "verification_error",
            "last_verified_at",
            "created_at",
            "updated_at",
        ]


class DnsZoneSerializer(serializers.ModelSerializer):
    provider_account_name = serializers.SerializerMethodField()

    def get_provider_account_name(self, obj):
        return obj.provider_account.name if obj.provider_account_id else ""

    class Meta:
        model = DnsZone
        fields = [
            "id",
            "name",
            "provider_account",
            "provider_account_name",
            "provider_zone_id",
            "is_active",
            "default_proxied",
            "last_synced_at",
            "created_at",
            "updated_at",
        ]


class DnsRecordSerializer(serializers.ModelSerializer):
    zone_name = serializers.SerializerMethodField()
    fqdn = serializers.SerializerMethodField()

    def get_zone_name(self, obj):
        return obj.zone.name if obj.zone_id else ""

    def get_fqdn(self, obj):
        return obj.fqdn

    class Meta:
        model = DnsRecord
        fields = [
            "id",
            "zone",
            "zone_name",
            "record_type",
            "hostname",
            "fqdn",
            "value",
            "ttl",
            "proxied",
            "provider_record_id",
            "status",
            "last_error",
            "last_synced_at",
            "created_at",
            "updated_at",
        ]


class DomainAssignmentSerializer(serializers.ModelSerializer):
    zone_name = serializers.SerializerMethodField()
    record_id = serializers.SerializerMethodField()
    instance_id = serializers.SerializerMethodField()
    instance_name = serializers.SerializerMethodField()
    domain_status = serializers.SerializerMethodField()
    ssl_status = serializers.SerializerMethodField()
    ssl_error = serializers.SerializerMethodField()
    source_label = serializers.SerializerMethodField()

    def get_zone_name(self, obj):
        return obj.zone.name if obj.zone_id else ""

    def get_record_id(self, obj):
        return obj.dns_record_id or None

    def get_instance_id(self, obj):
        return obj.instance_id or None

    def get_instance_name(self, obj):
        return obj.instance.name if obj.instance_id else ""

    def get_domain_status(self, obj):
        return obj.instance.domain_status if obj.instance_id else ""

    def get_ssl_status(self, obj):
        return obj.instance.ssl_status if obj.instance_id else ""

    def get_ssl_error(self, obj):
        return obj.instance.ssl_error if obj.instance_id else ""

    def get_source_label(self, obj):
        return obj.get_source_display()

    class Meta:
        model = DomainAssignment
        fields = [
            "id",
            "instance_id",
            "instance_name",
            "zone",
            "zone_name",
            "record_id",
            "domain",
            "hostname",
            "source",
            "source_label",
            "is_primary",
            "proxied",
            "is_managed",
            "provider_record_id",
            "status",
            "domain_status",
            "ssl_status",
            "ssl_error",
            "last_error",
            "last_synced_at",
            "created_at",
            "updated_at",
        ]
