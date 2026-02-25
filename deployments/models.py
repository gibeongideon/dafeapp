from django.conf import settings
from django.db import models


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
        READY = "READY", "Ready"
        FAILED = "FAILED", "Failed"
        DELETED = "DELETED", "Deleted"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="odoo_servers",
    )
    cloud_account = models.ForeignKey(
        "cloud.CloudAccount",
        on_delete=models.PROTECT,
        related_name="odoo_servers",
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
        unique_together = ("server", "db_name")
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["server", "status"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.db_name}) [{self.status}]"
