from django.conf import settings
from django.db import models


class Infrastructure(models.Model):
    """Connection layer for compute resources (no app deployment logic)."""

    class InfraType(models.TextChoices):
        PYOS = "PYOS", "PYOS (SSH / VPS)"
        MANAGED = "MANAGED", "Managed Cloud"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="deploy_infrastructure",
    )
    name = models.CharField(max_length=120)
    infra_type = models.CharField(max_length=15, choices=InfraType.choices)
    external_server = models.ForeignKey(
        "cloud.ExternalServer",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="deploy_infrastructure",
    )
    cloud_account = models.ForeignKey(
        "cloud.CloudAccount",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="deploy_infrastructure",
    )
    is_connected = models.BooleanField(default=False)
    validation_log = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_infrastructure",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = ("organization", "name")

    def __str__(self):
        return f"{self.name} ({self.get_infra_type_display()})"

    @property
    def managed_account(self):
        """Canonical managed-cloud account for this infrastructure."""
        return self.cloud_account if self.infra_type == self.InfraType.MANAGED else None

    def validate_connection_target(self) -> tuple[bool, str]:
        """
        Ensure the infrastructure points to the correct verified target.
        Keeps validation rules in one place for API and task code.
        """
        if self.infra_type == self.InfraType.MANAGED:
            if not self.cloud_account:
                return False, "Managed infrastructure requires a cloud account."
            if not self.cloud_account.is_verified:
                return False, "Managed infrastructure requires a verified cloud account."
            return True, ""
        if self.infra_type == self.InfraType.PYOS:
            if not self.external_server:
                return False, "PYOS infrastructure requires an external server."
            if not self.external_server.is_verified:
                return False, "PYOS infrastructure requires a verified external server."
            return True, ""
        return False, "Unsupported infrastructure type."


class Instance(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        STOPPED = "STOPPED", "Stopped"
        FAILED = "FAILED", "Failed"
        DELETED = "DELETED", "Deleted"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="instances",
    )
    cloud_account = models.ForeignKey(
        "cloud.CloudAccount",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="instances",
    )
    name = models.CharField(max_length=255)
    region = models.CharField(max_length=50, blank=True, default="")
    size = models.CharField(max_length=50, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    provisioning_log = models.TextField(blank=True)
    terraform_state_path = models.CharField(max_length=500, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_instances",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} [{self.status}] @ {self.organization.name}"


class TerraformRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"

    instance = models.OneToOneField(
        Instance, on_delete=models.CASCADE, related_name="terraform_run"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED
    )
    command = models.TextField(blank=True)
    output_log = models.TextField(blank=True)
    error_log = models.TextField(blank=True)
    state_file_path = models.CharField(max_length=500, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"TerraformRun #{self.pk} [{self.status}] for {self.instance.name}"


class OdooServer(models.Model):
    class OdooVersion(models.TextChoices):
        V18 = "18", "Odoo 18"
        V19 = "19", "Odoo 19"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROVISIONING = "PROVISIONING", "Provisioning"
        CONFIGURING = "CONFIGURING", "Configuring"
        PROVISIONED = "PROVISIONED", "Provisioned"
        FAILED = "FAILED", "Failed"
        DELETED = "DELETED", "Deleted"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="odoo_servers",
    )
    infrastructure = models.ForeignKey(
        Infrastructure,
        on_delete=models.PROTECT,
        related_name="servers",
        null=True,
        blank=True,
    )
    cloud_account = models.ForeignKey(
        "cloud.CloudAccount",
        on_delete=models.PROTECT,
        related_name="odoo_servers",
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=255)
    odoo_version = models.CharField(max_length=2, choices=OdooVersion.choices)
    region = models.CharField(max_length=50)
    size = models.CharField(max_length=50)
    provider_server_id = models.CharField(max_length=120, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    dns_domain = models.CharField(max_length=255, blank=True, default="")
    firewall_configured = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    max_instances = models.PositiveIntegerField(default=20)
    capacity_cpu_cores = models.PositiveIntegerField(default=4)
    capacity_ram_mb = models.PositiveIntegerField(default=8192)
    min_port = models.PositiveIntegerField(default=8069)
    max_port = models.PositiveIntegerField(default=8100)
    terraform_state_path = models.CharField(max_length=500, blank=True, default="")
    provisioning_log = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_odoo_servers",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "odoo_version"]),
            models.Index(fields=["organization", "status"]),
        ]

    def __str__(self):
        return f"{self.name} (Odoo {self.odoo_version}) [{self.status}]"

    @property
    def effective_cloud_account(self):
        """
        Single source for managed-cloud account resolution.
        Prefer infrastructure-managed account; fallback to direct server field.
        """
        infra = self.infrastructure
        if infra and infra.infra_type == Infrastructure.InfraType.MANAGED and infra.cloud_account:
            return infra.cloud_account
        return self.cloud_account


class OdooInstance(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CONFIGURING = "CONFIGURING", "Configuring"
        RUNNING = "RUNNING", "Running"
        FAILED = "FAILED", "Failed"
        DELETED = "DELETED", "Deleted"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="odoo_instances",
    )
    server = models.ForeignKey(
        OdooServer,
        on_delete=models.CASCADE,
        related_name="instances",
    )
    name = models.CharField(max_length=255)
    db_name = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, blank=True, default="")
    http_port = models.PositiveIntegerField(default=8069)
    requested_cpu_cores = models.PositiveIntegerField(default=1)
    requested_ram_mb = models.PositiveIntegerField(default=1024)
    systemd_service = models.CharField(max_length=255, blank=True, default="")
    nginx_site = models.CharField(max_length=255, blank=True, default="")
    ssl_enabled = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    provisioning_log = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_odoo_instances",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = (("server", "db_name"), ("server", "http_port"))
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["server", "status"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.db_name}) [{self.status}]"
