from django.db import models

from cloud.encryption import FieldEncryptor


class ExternalServer(models.Model):
    """A user-supplied VPS connected via SSH (PYOS mode)."""

    class AuthType(models.TextChoices):
        PASSWORD = "PASSWORD", "Password"
        DAFEAPP_KEY = "DAFEAPP_KEY", "DafeApp SSH Key (public key auth)"

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
        max_length=15, choices=AuthType.choices, default=AuthType.DAFEAPP_KEY
    )

    # Encrypted credential field — raw value is NEVER stored
    encrypted_password = models.TextField(blank=True)
    ssh_key_path = models.CharField(max_length=500, blank=True, default="")

    is_verified = models.BooleanField(default=False)
    is_prepared = models.BooleanField(default=False)
    is_reachable = models.BooleanField(default=False)
    last_checked_at = models.DateTimeField(null=True, blank=True)
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

    class DOAuthMethod(models.TextChoices):
        TOKEN = "TOKEN", "Personal Access Token"
        OAUTH = "OAUTH", "OAuth"

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
    # DigitalOcean OAuth fields
    do_auth_method = models.CharField(
        max_length=10, choices=DOAuthMethod.choices, default=DOAuthMethod.TOKEN
    )
    encrypted_do_oauth_token = models.TextField(blank=True)
    encrypted_do_oauth_refresh_token = models.TextField(blank=True)
    do_oauth_token_expiry = models.DateTimeField(null=True, blank=True)
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
        """Return the effective DigitalOcean token (OAuth or PAT)."""
        if self.do_auth_method == self.DOAuthMethod.OAUTH:
            return FieldEncryptor.decrypt(self.encrypted_do_oauth_token)
        return FieldEncryptor.decrypt(self.encrypted_api_token)

    @property
    def aws_access_key_id(self):
        return FieldEncryptor.decrypt(self.encrypted_aws_access_key_id)

    @property
    def aws_secret_access_key(self):
        return FieldEncryptor.decrypt(self.encrypted_aws_secret_access_key)

    @property
    def do_oauth_token(self):
        return FieldEncryptor.decrypt(self.encrypted_do_oauth_token)

    @property
    def do_oauth_refresh_token(self):
        return FieldEncryptor.decrypt(self.encrypted_do_oauth_refresh_token)

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

        raw_oauth_token = getattr(self, "_raw_do_oauth_token", None)
        if raw_oauth_token:
            self.encrypted_do_oauth_token = FieldEncryptor.encrypt(raw_oauth_token)
            self._raw_do_oauth_token = None

        raw_oauth_refresh = getattr(self, "_raw_do_oauth_refresh_token", None)
        if raw_oauth_refresh:
            self.encrypted_do_oauth_refresh_token = FieldEncryptor.encrypt(raw_oauth_refresh)
            self._raw_do_oauth_refresh_token = None

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


class PyOSSSHSettings(models.Model):
    """
    Singleton settings for PYOS SSH behavior.

    The default SSH key path is used when a server does not specify its own
    per-server path. If blank, DafeApp falls back to its managed SSH keypair.
    """

    default_ssh_key_path = models.CharField(max_length=500, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "PYOS SSH Settings"
        verbose_name_plural = "PYOS SSH Settings"

    def __str__(self):
        return "PYOS SSH Settings"

    @classmethod
    def get_or_create_settings(cls):
        obj = cls.objects.first()
        if obj:
            return obj
        return cls.objects.create(default_ssh_key_path="")


class SystemSSHKey(models.Model):
    """
    DafeApp's own SSH keypair — generated once and stored in the database.

    When a PYOS server uses 'DAFEAPP_KEY' auth, DafeApp connects using this
    keypair.  Users copy the public_key value into their server's
    ~/.ssh/authorized_keys — no file paths or local keys needed.
    """

    public_key = models.TextField(help_text="Add this to ~/.ssh/authorized_keys on your server.")
    encrypted_private_key = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "System SSH Key"
        verbose_name_plural = "System SSH Keys"

    def __str__(self):
        return f"DafeApp SSH Key (created {self.created_at:%Y-%m-%d})"

    @classmethod
    def get_or_create_keypair(cls):
        """
        Return the singleton SystemSSHKey, generating a new Ed25519 keypair
        if one does not exist yet.
        """
        import io
        import paramiko

        obj = cls.objects.first()
        if obj:
            return obj

        # Generate a new Ed25519 keypair via the cryptography library,
        # then load it into paramiko to get the OpenSSH public key string.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, NoEncryption, PrivateFormat,
        )

        raw_key = Ed25519PrivateKey.generate()
        private_key_str = raw_key.private_bytes(
            Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()
        ).decode()

        key = paramiko.Ed25519Key.from_private_key(io.StringIO(private_key_str))
        public_key_str = f"{key.get_name()} {key.get_base64()} dafeapp"

        obj = cls(
            public_key=public_key_str,
            encrypted_private_key=FieldEncryptor.encrypt(private_key_str),
        )
        obj.save()
        return obj

    def get_private_key(self):
        """Return the decrypted private key string."""
        return FieldEncryptor.decrypt(self.encrypted_private_key)
