from django.db import models

from cloud.encryption import FieldEncryptor


class ExternalServer(models.Model):
    """A user-supplied VPS connected via SSH (PYOS mode)."""

    class AuthType(models.TextChoices):
        SSH_KEY = "SSH_KEY", "SSH Private Key"
        PASSWORD = "PASSWORD", "Password"

    class PreparationStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        DONE = "DONE", "Done"
        FAILED = "FAILED", "Failed"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="external_servers",
    )
    name = models.CharField(max_length=100)
    host = models.GenericIPAddressField()
    port = models.PositiveIntegerField(default=22)
    username = models.CharField(max_length=100, default="root")
    auth_type = models.CharField(
        max_length=10, choices=AuthType.choices, default=AuthType.SSH_KEY
    )

    # Encrypted credential fields — raw values are NEVER stored
    encrypted_private_key = models.TextField(blank=True)
    encrypted_password = models.TextField(blank=True)

    is_verified = models.BooleanField(default=False)
    is_prepared = models.BooleanField(default=False)
    verification_error = models.CharField(max_length=500, blank=True)
    preparation_status = models.CharField(
        max_length=15,
        choices=PreparationStatus.choices,
        default=PreparationStatus.PENDING,
    )
    preparation_log = models.TextField(blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.host})"

    def save(self, *args, **kwargs):
        # Encrypt raw credentials set on the instance before persisting
        raw_key = getattr(self, "_raw_private_key", None)
        if raw_key:
            self.encrypted_private_key = FieldEncryptor.encrypt(raw_key)
            self._raw_private_key = None

        raw_password = getattr(self, "_raw_password", None)
        if raw_password:
            self.encrypted_password = FieldEncryptor.encrypt(raw_password)
            self._raw_password = None

        super().save(*args, **kwargs)


class CloudAccount(models.Model):
    """Cloud provider API credentials (e.g. DigitalOcean token)."""

    class Provider(models.TextChoices):
        DIGITALOCEAN = "DIGITALOCEAN", "DigitalOcean"
        AWS = "AWS", "Amazon Web Services"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="cloud_accounts",
    )
    provider = models.CharField(
        max_length=20, choices=Provider.choices, default=Provider.DIGITALOCEAN
    )
    name = models.CharField(max_length=100)
    encrypted_api_token = models.TextField(blank=True)
    encrypted_aws_access_key_id = models.TextField(blank=True)
    encrypted_aws_secret_access_key = models.TextField(blank=True)
    aws_default_region = models.CharField(max_length=30, blank=True, default="")
    is_verified = models.BooleanField(default=False)
    verification_error = models.CharField(max_length=500, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.get_provider_display()})"

    @property
    def api_token(self):
        return FieldEncryptor.decrypt(self.encrypted_api_token)

    @property
    def aws_access_key_id(self):
        return FieldEncryptor.decrypt(self.encrypted_aws_access_key_id)

    @property
    def aws_secret_access_key(self):
        return FieldEncryptor.decrypt(self.encrypted_aws_secret_access_key)

    def save(self, *args, **kwargs):
        raw_token = getattr(self, "_raw_api_token", None)
        if raw_token:
            self.encrypted_api_token = FieldEncryptor.encrypt(raw_token)
            self._raw_api_token = None

        raw_aws_key = getattr(self, "_raw_aws_access_key_id", None)
        if raw_aws_key:
            self.encrypted_aws_access_key_id = FieldEncryptor.encrypt(raw_aws_key)
            self._raw_aws_access_key_id = None

        raw_aws_secret = getattr(self, "_raw_aws_secret_access_key", None)
        if raw_aws_secret:
            self.encrypted_aws_secret_access_key = FieldEncryptor.encrypt(raw_aws_secret)
            self._raw_aws_secret_access_key = None
        super().save(*args, **kwargs)


class CloudServer(models.Model):
    """A provisioned Droplet (or similar) managed by DafeApp."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROVISIONING = "PROVISIONING", "Provisioning"
        RUNNING = "RUNNING", "Running"
        STOPPED = "STOPPED", "Stopped"
        FAILED = "FAILED", "Failed"
        DELETED = "DELETED", "Deleted"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="cloud_servers",
    )
    cloud_account = models.ForeignKey(
        CloudAccount, on_delete=models.PROTECT, related_name="servers"
    )
    name = models.CharField(max_length=100)
    provider_server_id = models.CharField(max_length=100, blank=True)
    region = models.CharField(max_length=50)
    size = models.CharField(max_length=50)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    status = models.CharField(
        max_length=15, choices=Status.choices, default=Status.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} [{self.status}]"


class Infrastructure(models.Model):
    """Unified reference to either a PYOS server or a managed cloud server."""

    class InfraType(models.TextChoices):
        PYOS = "PYOS", "PYOS (Own VPS)"
        MANAGED = "MANAGED", "Managed (DigitalOcean)"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="infrastructure",
    )
    infra_type = models.CharField(max_length=10, choices=InfraType.choices)
    external_server = models.OneToOneField(
        ExternalServer,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="infrastructure",
    )
    cloud_server = models.OneToOneField(
        CloudServer,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="infrastructure",
    )
    name = models.CharField(max_length=100)
    is_ready = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.get_infra_type_display()})"
