from django.conf import settings
from django.db import models
from django.db.models import Q

from cloud.encryption import FieldEncryptor


def normalize_domain_name(value: str) -> str:
    return (value or "").strip().strip(".").lower()


def normalize_record_hostname(value: str) -> str:
    normalized = (value or "@").strip().strip(".").lower()
    return normalized or "@"


def hostname_for_domain(domain: str, zone_name: str) -> str:
    normalized_domain = normalize_domain_name(domain)
    normalized_zone = normalize_domain_name(zone_name)
    if not normalized_domain or not normalized_zone:
        return "@"
    if normalized_domain == normalized_zone:
        return "@"
    suffix = f".{normalized_zone}"
    if normalized_domain.endswith(suffix):
        return normalized_domain[: -len(suffix)]
    return normalized_domain


def domain_belongs_to_zone(domain: str, zone_name: str) -> bool:
    normalized_domain = normalize_domain_name(domain)
    normalized_zone = normalize_domain_name(zone_name)
    return bool(
        normalized_domain
        and normalized_zone
        and (normalized_domain == normalized_zone or normalized_domain.endswith(f".{normalized_zone}"))
    )


class DnsProviderAccount(models.Model):
    class Provider(models.TextChoices):
        CLOUDFLARE = "CLOUDFLARE", "Cloudflare"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="dns_provider_accounts",
    )
    name = models.CharField(max_length=120)
    provider = models.CharField(
        max_length=20,
        choices=Provider.choices,
        default=Provider.CLOUDFLARE,
    )
    encrypted_api_token = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False)
    verification_error = models.CharField(max_length=500, blank=True, default="")
    last_verified_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_dns_provider_accounts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="dns_provider_account_org_name_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "provider"], name="dns_provider_org_type_idx"),
            models.Index(fields=["organization", "is_active"], name="dns_provider_org_active_idx"),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_provider_display()})"

    @property
    def api_token(self) -> str:
        return FieldEncryptor.decrypt(self.encrypted_api_token)

    @property
    def token_configured(self) -> bool:
        return bool(self.encrypted_api_token)

    def save(self, *args, **kwargs):
        raw_api_token = getattr(self, "_raw_api_token", None)
        if raw_api_token is not None:
            self.encrypted_api_token = FieldEncryptor.encrypt(raw_api_token or "")
            self._raw_api_token = None
        super().save(*args, **kwargs)


class DnsZone(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="dns_zones",
    )
    provider_account = models.ForeignKey(
        DnsProviderAccount,
        on_delete=models.CASCADE,
        related_name="zones",
    )
    name = models.CharField(max_length=255)
    provider_zone_id = models.CharField(max_length=255, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    default_proxied = models.BooleanField(default=False)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="dns_zone_org_name_uniq",
            ),
            models.UniqueConstraint(
                fields=["provider_account", "provider_zone_id"],
                condition=Q(provider_zone_id__isnull=False),
                name="dns_zone_provider_zone_id_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "name"], name="dns_zone_org_name_idx"),
            models.Index(fields=["provider_account", "is_active"], name="dns_zone_provider_active_idx"),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.name = normalize_domain_name(self.name)
        if self.provider_zone_id == "":
            self.provider_zone_id = None
        super().save(*args, **kwargs)

    @classmethod
    def match_for_domain(cls, organization, domain: str, preferred_zone=None):
        normalized_domain = normalize_domain_name(domain)
        if not normalized_domain or organization is None:
            return None

        zones = list(
            cls.objects.filter(organization=organization, is_active=True).select_related("provider_account")
        )
        matches = [zone for zone in zones if domain_belongs_to_zone(normalized_domain, zone.name)]
        if preferred_zone and domain_belongs_to_zone(normalized_domain, preferred_zone.name):
            matches.append(preferred_zone)
        if not matches:
            return None
        return max(matches, key=lambda zone: len(zone.name))

    def hostname_for_domain(self, domain: str) -> str:
        return hostname_for_domain(domain, self.name)


class DnsRecord(models.Model):
    class RecordType(models.TextChoices):
        A = "A", "A"
        AAAA = "AAAA", "AAAA"
        CNAME = "CNAME", "CNAME"
        TXT = "TXT", "TXT"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACTIVE = "ACTIVE", "Active"
        FAILED = "FAILED", "Failed"
        DELETED = "DELETED", "Deleted"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="dns_records",
    )
    zone = models.ForeignKey(
        DnsZone,
        on_delete=models.CASCADE,
        related_name="records",
    )
    record_type = models.CharField(
        max_length=10,
        choices=RecordType.choices,
        default=RecordType.A,
    )
    hostname = models.CharField(max_length=255, default="@")
    value = models.CharField(max_length=255)
    ttl = models.PositiveIntegerField(default=1)
    proxied = models.BooleanField(default=False)
    provider_record_id = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.PENDING,
    )
    last_error = models.TextField(blank=True, default="")
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["zone__name", "hostname", "record_type", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["zone", "record_type", "hostname"],
                condition=~Q(status="DELETED"),
                name="dns_active_record_zone_type_host_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="dns_record_org_status_idx"),
            models.Index(fields=["zone", "hostname"], name="dns_record_zone_host_idx"),
            models.Index(fields=["zone", "provider_record_id"], name="dns_record_zone_provider_idx"),
        ]

    def __str__(self):
        return f"{self.fqdn} ({self.record_type})"

    def save(self, *args, **kwargs):
        self.hostname = normalize_record_hostname(self.hostname)
        self.value = (self.value or "").strip()
        super().save(*args, **kwargs)

    @property
    def fqdn(self) -> str:
        if self.hostname == "@":
            return self.zone.name
        return f"{self.hostname}.{self.zone.name}"


class DomainAssignment(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACTIVE = "ACTIVE", "Active"
        FAILED = "FAILED", "Failed"
        DELETED = "DELETED", "Deleted"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="domain_assignments",
    )
    instance = models.ForeignKey(
        "deployments.OdooInstance",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="domain_assignments",
    )
    zone = models.ForeignKey(
        DnsZone,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assignments",
    )
    dns_record = models.ForeignKey(
        DnsRecord,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="domain_assignments",
    )
    domain = models.CharField(max_length=255)
    hostname = models.CharField(max_length=255, default="@")
    proxied = models.BooleanField(default=False)
    is_managed = models.BooleanField(default=False)
    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.PENDING,
    )
    last_error = models.TextField(blank=True, default="")
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["domain"],
                condition=~Q(status="DELETED"),
                name="dns_active_assignment_domain_uniq",
            ),
            models.UniqueConstraint(
                fields=["instance"],
                condition=~Q(status="DELETED"),
                name="dns_active_assignment_instance_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "domain"], name="dns_assign_org_domain_idx"),
            models.Index(fields=["zone", "hostname"], name="dns_assign_zone_host_idx"),
            models.Index(fields=["organization", "status"], name="dns_assign_org_status_idx"),
        ]

    def __str__(self):
        return self.domain

    def save(self, *args, **kwargs):
        self.domain = normalize_domain_name(self.domain)
        self.hostname = normalize_record_hostname(self.hostname)
        if self.zone_id and self.domain:
            self.hostname = self.zone.hostname_for_domain(self.domain)
        super().save(*args, **kwargs)
