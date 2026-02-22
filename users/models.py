import uuid

from django.conf import settings
from django.db import models
from django.contrib.auth.models import AbstractUser

from .managers import UserManager


class User(AbstractUser):
    # Override username: optional, auto-generated, not unique
    username = models.CharField(max_length=150, blank=True)
    # Make email the login field + unique
    email = models.EmailField(unique=True)

    # Platform-level admin (LaunchPad staff — can see ALL orgs)
    is_platform_admin = models.BooleanField(default=False)

    # Email verification
    is_email_verified = models.BooleanField(default=False)
    email_verification_token = models.UUIDField(
        default=uuid.uuid4, editable=False, unique=True
    )

    # Social auth provider (email = standard password login)
    auth_provider = models.CharField(max_length=30, blank=True, default="email")

    # Login tracking
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    login_count = models.PositiveIntegerField(default=0)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # nothing extra beyond email/password for createsuperuser

    objects = UserManager()

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"

    def __str__(self):
        return self.email

    def get_full_name(self):
        full = f"{self.first_name} {self.last_name}".strip()
        return full or self.email

    def get_short_name(self):
        return self.first_name or self.email.split("@")[0]

    @property
    def display_name(self):
        return self.get_full_name()

    def membership_for(self, organization):
        """Return active membership for a given org, or None."""
        try:
            return self.memberships.get(organization=organization, is_active=True)
        except self.memberships.model.DoesNotExist:
            return None


class VCSAccount(models.Model):
    """
    Stores an encrypted OAuth access token for GitHub/GitLab API access.
    Separate from allauth's SocialAccount — this is used for VCS operations
    (pulling repos, triggering deployments) rather than for authentication.
    """

    class Provider(models.TextChoices):
        GITHUB = "github", "GitHub"
        GITLAB = "gitlab", "GitLab"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="vcs_accounts",
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    username = models.CharField(max_length=255, blank=True)
    encrypted_access_token = models.TextField()
    connected_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("user", "provider")
        ordering = ["provider"]

    def __str__(self):
        return f"{self.user.email} @ {self.provider} ({self.username})"

    @property
    def access_token(self):
        from cloud.encryption import FieldEncryptor
        return FieldEncryptor.decrypt(self.encrypted_access_token)

    @access_token.setter
    def access_token(self, value):
        from cloud.encryption import FieldEncryptor
        self.encrypted_access_token = FieldEncryptor.encrypt(value)
