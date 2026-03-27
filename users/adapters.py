"""
Custom django-allauth adapters.

AccountAdapter  — redirects to /dashboard/ after social login.
SocialAccountAdapter — on first social signup: connects existing user by email
                       OR auto-creates a User + Organization + SUPER_ADMIN membership.
                       Also populates auth_provider on the User model.
"""

import logging

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.db import transaction

logger = logging.getLogger(__name__)


class AccountAdapter(DefaultAccountAdapter):
    """Standard account adapter; redirect social logins to the dashboard."""

    def get_login_redirect_url(self, request):
        return "/dashboard/"


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """
    Handles two edge cases in the social-login pipeline:

    1. Email collision — if an existing DafeApp user's email matches the social
       provider's email, connect the social account to that existing user rather
       than raising a duplicate-email error.

    2. New user via social login — auto-create an Organization named after the
       user and make them SUPER_ADMIN, mirroring the OrgSignupView flow.
    """

    def pre_social_login(self, request, sociallogin):
        """Connect social account to an existing user whose email matches."""
        from django.contrib.auth import get_user_model

        if sociallogin.is_existing:
            return

        email = sociallogin.user.email
        if not email:
            return

        User = get_user_model()
        try:
            existing = User.objects.get(email=email)
            sociallogin.connect(request, existing)
        except User.DoesNotExist:
            pass

    def on_authentication_error(
        self,
        request,
        provider,
        error=None,
        exception=None,
        extra_context=None,
    ):
        logger.warning(
            "Social auth error: provider=%s error=%s exception=%r path=%s user=%s session_key=%s state_id=%s session_states=%s",
            getattr(provider, "id", provider),
            error,
            exception,
            getattr(request, "path", ""),
            getattr(getattr(request, "user", None), "email", None),
            getattr(getattr(request, "session", None), "session_key", None),
            (extra_context or {}).get("state_id"),
            list((getattr(request, "session", {}) or {}).get("socialaccount_states", {}).keys())
            if hasattr(request, "session")
            else [],
        )
        return super().on_authentication_error(
            request,
            provider,
            error=error,
            exception=exception,
            extra_context=extra_context,
        )

    def save_user(self, request, sociallogin, form=None):
        """
        Called only for NEW users (first social login with an unknown email).
        Creates the user via allauth, then builds their first Organization.
        """
        from audit.models import AuditLog
        from core.utils import get_client_ip
        from organizations.models import Organization, OrganizationMembership

        user = super().save_user(request, sociallogin, form)

        # Mark auth provider and auto-verify email (OAuth provider already did it)
        user.auth_provider = sociallogin.account.provider
        user.is_email_verified = True
        user.save(update_fields=["auth_provider", "is_email_verified"])

        # Create org if this user has none yet
        if not user.memberships.filter(is_active=True).exists():
            display = user.get_full_name() or user.email.split("@")[0]
            org_name = f"{display}'s Organization"
            with transaction.atomic():
                org = Organization.objects.create(name=org_name, owner=user)
                OrganizationMembership.objects.create(
                    user=user,
                    organization=org,
                    role=OrganizationMembership.Role.SUPER_ADMIN,
                )
                ip = get_client_ip(request) if request else None
                AuditLog.objects.create(
                    user=user,
                    organization=org,
                    action=AuditLog.Action.REGISTER,
                    ip_address=ip,
                    description=f"Social signup via {user.auth_provider}",
                )
                AuditLog.objects.create(
                    user=user,
                    organization=org,
                    action=AuditLog.Action.ORG_CREATED,
                    ip_address=ip,
                    description=f"Organization '{org_name}' auto-created via social auth",
                )

        return user

    def is_auto_signup_allowed(self, request, sociallogin):
        """Allow automatic account creation on first social login."""
        return True
