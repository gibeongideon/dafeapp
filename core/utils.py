def get_client_ip(request):
    """Extract the real client IP from request headers."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def log_audit(user, action, request=None, description="", metadata=None):
    """Helper to create an AuditLog entry anywhere in the app."""
    from audit.models import AuditLog

    ip = get_client_ip(request) if request else None
    ua = request.META.get("HTTP_USER_AGENT", "") if request else ""
    AuditLog.objects.create(
        user=user,
        action=action,
        description=description,
        ip_address=ip,
        user_agent=ua,
        metadata=metadata or {},
    )
