from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect


def organization_required(view_func):
    """Ensure user has a current organization set in request context."""
    @wraps(view_func)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.organization:
            return redirect("organizations:select")
        return view_func(request, *args, **kwargs)
    return wrapper


def organization_role_required(allowed_roles):
    """
    Restrict view to users whose current org role is in `allowed_roles`.

    Usage:
        @organization_role_required(["SUPER_ADMIN", "ADMIN"])
        def my_view(request): ...
    """
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def wrapper(request, *args, **kwargs):
            if not request.organization:
                return redirect("organizations:select")
            if request.org_role not in allowed_roles:
                return HttpResponseForbidden(
                    "You don't have permission to access this resource."
                )
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


def platform_admin_required(view_func):
    """Restrict to LaunchPad platform admins (is_platform_admin=True)."""
    @wraps(view_func)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_platform_admin:
            return HttpResponseForbidden("Platform admin access required.")
        return view_func(request, *args, **kwargs)
    return wrapper
