from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.db.models import F
from django.dispatch import receiver

from core.utils import get_client_ip


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
