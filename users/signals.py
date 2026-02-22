from allauth.socialaccount.signals import social_account_added, social_account_updated
from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.db.models import F
from django.dispatch import receiver

from core.utils import get_client_ip


# ─── VCS Account Sync ────────────────────────────────────────────────────────

def _sync_vcs_account(sociallogin):
    """
    When a GitHub or GitLab account is connected, persist the OAuth access token
    into VCSAccount (Fernet-encrypted). Also writes an audit log entry.
    Called from both social_account_added and social_account_updated.
    """
    provider = sociallogin.account.provider
    if provider not in ("github", "gitlab"):
        return

    token_obj = getattr(sociallogin, "token", None)
    if not token_obj or not token_obj.token:
        return

    from audit.models import AuditLog
    from cloud.encryption import FieldEncryptor

    from .models import VCSAccount

    extra = sociallogin.account.extra_data or {}
    username = extra.get("login") or extra.get("username", "")
    encrypted = FieldEncryptor.encrypt(token_obj.token)

    _, created = VCSAccount.objects.update_or_create(
        user=sociallogin.user,
        provider=provider,
        defaults={
            "username": username,
            "encrypted_access_token": encrypted,
            "is_active": True,
        },
    )

    verb = "Connected" if created else "Re-connected"
    AuditLog.objects.create(
        user=sociallogin.user,
        action=AuditLog.Action.VCS_CONNECT,
        description=f"{verb} {provider} VCS account: {username}",
    )


@receiver(social_account_added)
def on_social_account_added(sender, request, sociallogin, **kwargs):
    _sync_vcs_account(sociallogin)


@receiver(social_account_updated)
def on_social_account_updated(sender, request, sociallogin, **kwargs):
    _sync_vcs_account(sociallogin)


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    from audit.models import AuditLog

    ip = get_client_ip(request)
    user.last_login_ip = ip
    user.login_count = F("login_count") + 1
    user.save(update_fields=["last_login_ip", "login_count"])

    org = getattr(request, "organization", None)
    AuditLog.objects.create(
        user=user,
        organization=org,
        action=AuditLog.Action.LOGIN,
        ip_address=ip,
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        description=f"Logged in from {ip}",
    )


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    from audit.models import AuditLog

    if user:
        org = getattr(request, "organization", None)
        AuditLog.objects.create(
            user=user,
            organization=org,
            action=AuditLog.Action.LOGOUT,
            ip_address=get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request, **kwargs):
    from audit.models import AuditLog

    AuditLog.objects.create(
        action=AuditLog.Action.LOGIN_FAILED,
        ip_address=get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        description=f"Failed login for {credentials.get('email', '?')}",
        metadata={"email": credentials.get("email", "")},
    )
