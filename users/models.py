import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models

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
