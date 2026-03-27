from django.conf import settings
from django.db import models

from cloud.encryption import FieldEncryptor


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
        V17 = "17", "Odoo 17"
        V18 = "18", "Odoo 18"
        V19 = "19", "Odoo 19"

    class DeploymentMode(models.TextChoices):
        BARE_METAL = "BARE_METAL", "Bare-metal (systemd)"
        DOCKER = "DOCKER", "Docker (Traefik + containers)"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROVISIONING = "PROVISIONING", "Provisioning"
        CONFIGURING = "CONFIGURING", "Configuring"
        PROVISIONED = "PROVISIONED", "Provisioned"
        FAILED = "FAILED", "Failed"
        ARCHIVED = "ARCHIVED", "Archived"
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
    deployment_mode = models.CharField(
        max_length=15,
        choices=DeploymentMode.choices,
        default=DeploymentMode.BARE_METAL,
    )
    is_active = models.BooleanField(default=True)
    docker_postgres_password = models.CharField(max_length=255, blank=True, default="")
    terraform_state_path = models.CharField(max_length=500, blank=True, default="")
    provisioning_log = models.TextField(blank=True)
    installation_summary = models.JSONField(default=dict, blank=True)
    installation_summary_text = models.TextField(blank=True)
    is_reachable = models.BooleanField(default=False)
    last_checked_at = models.DateTimeField(null=True, blank=True)
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

    @property
    def active_instance_count(self):
        return self.instances.exclude(status=OdooInstance.Status.DELETED).count()


class OdooInstance(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CONFIGURING = "CONFIGURING", "Configuring"
        RUNNING = "RUNNING", "Running"
        FAILED = "FAILED", "Failed"
        DELETED = "DELETED", "Deleted"

    class AddonsSyncStatus(models.TextChoices):
        NOT_CONFIGURED = "NOT_CONFIGURED", "Not configured"
        PENDING = "PENDING", "Pending"
        READY = "READY", "Ready"
        ERROR = "ERROR", "Error"

    class RestartPolicy(models.TextChoices):
        ALWAYS = "always", "Always"
        ON_FAILURE = "on-failure", "On Failure"
        NO = "no", "No"

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
    container_name = models.CharField(max_length=255, blank=True, default="")
    systemd_service = models.CharField(max_length=255, blank=True, default="")
    nginx_site = models.CharField(max_length=255, blank=True, default="")
    ssl_enabled = models.BooleanField(default=False)
    restart_policy = models.CharField(
        max_length=15, choices=RestartPolicy.choices, default=RestartPolicy.ALWAYS
    )
    is_reachable = models.BooleanField(default=False)
    last_health_check = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    provisioning_log = models.TextField(blank=True)
    installation_summary = models.JSONField(default=dict, blank=True)
    installation_summary_text = models.TextField(blank=True)
    addons_root_path = models.CharField(max_length=500, blank=True, default="")
    addons_path_cache = models.TextField(blank=True, default="")
    addons_sync_status = models.CharField(
        max_length=20,
        choices=AddonsSyncStatus.choices,
        default=AddonsSyncStatus.NOT_CONFIGURED,
    )
    addons_last_sync_at = models.DateTimeField(null=True, blank=True)
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

    @property
    def access_url(self) -> str:
        """Direct IP:PORT access URL — empty if server IP not yet assigned."""
        ip = self.server.ip_address if self.server_id else None
        if ip:
            return f"http://{ip}:{self.http_port}"
        return ""

    @property
    def storage_path(self) -> str:
        """Best-effort storage path for UI rendering."""
        summary = self.installation_summary or {}
        return summary.get("data_dir") or summary.get("instance_dir") or ""

    def __str__(self):
        return f"{self.name} ({self.db_name}) [{self.status}]"


class GitRepositoryCredential(models.Model):
    class AuthType(models.TextChoices):
        PUBLIC = "PUBLIC", "Public"
        GITHUB_OAUTH = "GITHUB_OAUTH", "GitHub OAuth"
        TOKEN = "TOKEN", "Personal access token"
        SSH_KEY = "SSH_KEY", "SSH key"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="git_repo_credentials",
    )
    name = models.CharField(max_length=120)
    auth_type = models.CharField(
        max_length=20,
        choices=AuthType.choices,
        default=AuthType.PUBLIC,
    )
    github_account = models.ForeignKey(
        "users.VCSAccount",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deployment_git_credentials",
    )
    git_username = models.CharField(max_length=255, blank=True, default="")
    encrypted_access_token = models.TextField(blank=True, default="")
    encrypted_ssh_private_key = models.TextField(blank=True, default="")
    encrypted_ssh_key_passphrase = models.TextField(blank=True, default="")
    ssh_public_key = models.TextField(blank=True, default="")
    notes = models.CharField(max_length=255, blank=True, default="")
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_git_repo_credentials",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        unique_together = (("organization", "name"),)
        indexes = [
            models.Index(fields=["organization", "auth_type"], name="dep_git_cred_org_type_idx"),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_auth_type_display()})"

    @property
    def access_token(self) -> str:
        if self.auth_type == self.AuthType.GITHUB_OAUTH and self.github_account_id:
            return self.github_account.access_token
        return FieldEncryptor.decrypt(self.encrypted_access_token)

    @access_token.setter
    def access_token(self, value: str):
        self.encrypted_access_token = FieldEncryptor.encrypt(value or "")

    @property
    def ssh_private_key(self) -> str:
        return FieldEncryptor.decrypt(self.encrypted_ssh_private_key)

    @ssh_private_key.setter
    def ssh_private_key(self, value: str):
        self.encrypted_ssh_private_key = FieldEncryptor.encrypt(value or "")

    @property
    def ssh_key_passphrase(self) -> str:
        return FieldEncryptor.decrypt(self.encrypted_ssh_key_passphrase)

    @ssh_key_passphrase.setter
    def ssh_key_passphrase(self, value: str):
        self.encrypted_ssh_key_passphrase = FieldEncryptor.encrypt(value or "")

    def save(self, *args, **kwargs):
        # For GitHub OAuth, the token source of truth lives on users.VCSAccount.
        # Keep deployment credentials as metadata/reference rows only.
        if self.auth_type == self.AuthType.GITHUB_OAUTH:
            self.encrypted_access_token = ""
            if self.github_account_id and not self.git_username:
                self.git_username = self.github_account.username or self.git_username

        raw_token = getattr(self, "_raw_access_token", None)
        if raw_token is not None:
            self.access_token = raw_token
            self._raw_access_token = None

        raw_ssh_private_key = getattr(self, "_raw_ssh_private_key", None)
        if raw_ssh_private_key is not None:
            self.ssh_private_key = raw_ssh_private_key
            self._raw_ssh_private_key = None

        raw_passphrase = getattr(self, "_raw_ssh_key_passphrase", None)
        if raw_passphrase is not None:
            self.ssh_key_passphrase = raw_passphrase
            self._raw_ssh_key_passphrase = None
        super().save(*args, **kwargs)


class OdooInstanceGitRepo(models.Model):
    class AuthType(models.TextChoices):
        PUBLIC = "PUBLIC", "Public"
        GITHUB_OAUTH = "GITHUB_OAUTH", "GitHub OAuth"
        TOKEN = "TOKEN", "Personal access token"
        SSH_KEY = "SSH_KEY", "SSH key"

    class Status(models.TextChoices):
        CONNECTED = "CONNECTED", "Connected"
        CLONING = "CLONING", "Cloning"
        UPDATING = "UPDATING", "Updating"
        ERROR = "ERROR", "Error"
        DISCONNECTED = "DISCONNECTED", "Disconnected"

    instance = models.ForeignKey(
        OdooInstance,
        on_delete=models.CASCADE,
        related_name="git_repos",
    )
    credential = models.ForeignKey(
        GitRepositoryCredential,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="git_repos",
    )
    repo_name = models.CharField(max_length=255)
    git_url = models.CharField(max_length=500)
    branch = models.CharField(max_length=120, default="main")
    auth_type = models.CharField(
        max_length=20,
        choices=AuthType.choices,
        default=AuthType.PUBLIC,
    )
    local_path = models.CharField(max_length=500, blank=True, default="")
    auto_update = models.BooleanField(default=False)
    is_enabled = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)
    default_branch = models.CharField(max_length=120, blank=True, default="")
    pinned_commit = models.CharField(max_length=64, blank=True, default="")
    previous_commit = models.CharField(max_length=64, blank=True, default="")
    last_remote_commit = models.CharField(max_length=64, blank=True, default="")
    last_pulled_commit = models.CharField(max_length=64, blank=True, default="")
    last_pulled_at = models.DateTimeField(null=True, blank=True)
    last_sync_started_at = models.DateTimeField(null=True, blank=True)
    last_sync_finished_at = models.DateTimeField(null=True, blank=True)
    last_sync_log = models.TextField(blank=True, default="")
    last_detected_modules = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DISCONNECTED,
    )
    last_error = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_odoo_instance_git_repos",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "repo_name", "id"]
        unique_together = (("instance", "repo_name"),)
        indexes = [
            models.Index(fields=["instance", "status"], name="dep_repo_inst_status_idx"),
            models.Index(fields=["instance", "auto_update"], name="dep_repo_inst_auto_idx"),
        ]

    def __str__(self):
        return f"{self.repo_name} [{self.branch}] -> {self.instance.name}"


# ---------------------------------------------------------------------------
# Phase 2: Deployment Jobs, Version History
# ---------------------------------------------------------------------------

class DeploymentJob(models.Model):
    """Tracks every async Celery deployment operation for status, logs, and cancellation."""

    class JobType(models.TextChoices):
        PROVISION_SERVER = "PROVISION_SERVER", "Provision Server"
        CONFIGURE_SERVER = "CONFIGURE_SERVER", "Configure Server"
        CREATE_INSTANCE = "CREATE_INSTANCE", "Create Instance"
        DELETE_INSTANCE = "DELETE_INSTANCE", "Delete Instance"
        ROLLBACK_INSTANCE = "ROLLBACK_INSTANCE", "Rollback Instance"
        CLONE_INSTANCE_REPO = "CLONE_INSTANCE_REPO", "Clone Instance Repo"
        UPDATE_INSTANCE_REPO = "UPDATE_INSTANCE_REPO", "Update Instance Repo"
        CHECKOUT_INSTANCE_REPO_BRANCH = "CHECKOUT_INSTANCE_REPO_BRANCH", "Checkout Instance Repo Branch"
        REMOVE_INSTANCE_REPO = "REMOVE_INSTANCE_REPO", "Remove Instance Repo"
        REFRESH_INSTANCE_ADDONS = "REFRESH_INSTANCE_ADDONS", "Refresh Instance Addons"
        ROLLBACK_INSTANCE_REPO = "ROLLBACK_INSTANCE_REPO", "Rollback Instance Repo"
        AUTO_SYNC_INSTANCE_REPOS = "AUTO_SYNC_INSTANCE_REPOS", "Auto Sync Instance Repos"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        DONE = "DONE", "Done"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="deployment_jobs",
    )
    job_type = models.CharField(max_length=30, choices=JobType.choices)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.QUEUED)
    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    odoo_server = models.ForeignKey(
        OdooServer,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="jobs",
    )
    odoo_instance = models.ForeignKey(
        OdooInstance,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="jobs",
    )
    log = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="deployment_jobs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "-created_at"]),
        ]

    def __str__(self):
        return f"DeploymentJob #{self.pk} [{self.job_type}] {self.status}"


class ServerSSHKey(models.Model):
    """
    An additional public SSH key registered on an OdooServer.
    DafeApp's own key is always present; these are extra keys for team members
    or other machines that need direct SSH access.
    """

    server = models.ForeignKey(
        OdooServer,
        on_delete=models.CASCADE,
        related_name="ssh_keys",
    )
    label = models.CharField(max_length=120, help_text="Human-friendly name, e.g. 'Alice MacBook'")
    public_key = models.TextField(help_text="Full public key string (ssh-ed25519 / ssh-rsa …)")
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="added_server_ssh_keys",
    )
    deployed = models.BooleanField(
        default=False,
        help_text="True once the key has been written to the server's authorized_keys.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = ("server", "public_key")

    def __str__(self):
        return f"{self.label} → {self.server.name}"


class OdooServerHistory(models.Model):
    """Immutable snapshot of an OdooServer's state at each successful provision/configure."""

    server = models.ForeignKey(
        OdooServer,
        on_delete=models.CASCADE,
        related_name="history",
    )
    odoo_version = models.CharField(max_length=2)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    dns_domain = models.CharField(max_length=255, blank=True)
    region = models.CharField(max_length=50, blank=True)
    size = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=20)
    note = models.CharField(max_length=255, blank=True)
    deployed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="server_deploys",
    )
    deployed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-deployed_at"]

    def __str__(self):
        return f"Server #{self.server_id} v{self.odoo_version} @ {self.deployed_at:%Y-%m-%d %H:%M}"


class OdooInstanceHistory(models.Model):
    """Immutable snapshot of an OdooInstance's config at each successful deployment."""

    instance = models.ForeignKey(
        OdooInstance,
        on_delete=models.CASCADE,
        related_name="history",
    )
    db_name = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, blank=True)
    http_port = models.PositiveIntegerField()
    odoo_version = models.CharField(max_length=2)
    server_ip = models.GenericIPAddressField(null=True, blank=True)
    systemd_service = models.CharField(max_length=255, blank=True)
    ssl_enabled = models.BooleanField(default=False)
    status = models.CharField(max_length=20)
    note = models.CharField(max_length=255, blank=True)
    deployed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="instance_deploys",
    )
    deployed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-deployed_at"]

    def __str__(self):
        return f"Instance #{self.instance_id} ({self.db_name}) @ {self.deployed_at:%Y-%m-%d %H:%M}"
