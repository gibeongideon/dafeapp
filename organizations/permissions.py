"""
RBAC Permission Matrix
======================
Action              SUPER_ADMIN  ADMIN  MANAGER  USER
----------------------------------------------------------
create_user             ✅        ✅      ❌      ❌
delete_user             ✅        ❌      ❌      ❌
invite_user             ✅        ✅      ❌      ❌
change_role             ✅        ❌      ❌      ❌
disable_user            ✅        ✅      ❌      ❌
create_instance         ✅        ✅      ✅      ❌
delete_instance         ✅        ❌      ❌      ❌
deploy_odoo             ✅        ✅      ✅      ❌
view_logs               ✅        ✅      ✅      ✅
manage_billing          ✅        ❌      ❌      ❌
manage_organization     ✅        ❌      ❌      ❌
"""

from .models import OrganizationMembership

PERMISSION_MATRIX = {
    "create_user":          ["SUPER_ADMIN", "ADMIN"],
    "delete_user":          ["SUPER_ADMIN"],
    "invite_user":          ["SUPER_ADMIN", "ADMIN"],
    "change_role":          ["SUPER_ADMIN"],
    "disable_user":         ["SUPER_ADMIN", "ADMIN"],
    "create_instance":      ["SUPER_ADMIN", "ADMIN", "MANAGER"],
    "delete_instance":      ["SUPER_ADMIN"],
    "deploy_odoo":          ["SUPER_ADMIN", "ADMIN", "MANAGER"],
    "view_logs":            ["SUPER_ADMIN", "ADMIN", "MANAGER", "USER"],
    "manage_billing":       ["SUPER_ADMIN"],
    "manage_organization":  ["SUPER_ADMIN"],
}


def has_org_permission(user, organization, permission):
    """
    Returns True if `user` has `permission` within `organization`.
    Always checks via active membership — never trust a cached role.
    """
    try:
        membership = OrganizationMembership.objects.get(
            user=user, organization=organization, is_active=True
        )
        return membership.role in PERMISSION_MATRIX.get(permission, [])
    except OrganizationMembership.DoesNotExist:
        return False


# DRF permission classes
from rest_framework import permissions as drf_permissions  # noqa: E402


class IsOrgMember(drf_permissions.BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and getattr(request, "organization", None) is not None
        )


class IsOrgAdmin(drf_permissions.BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and getattr(request, "org_role", None) in ["SUPER_ADMIN", "ADMIN"]
        )


class IsOrgSuperAdmin(drf_permissions.BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and getattr(request, "org_role", None) == "SUPER_ADMIN"
        )


class IsPlatformAdmin(drf_permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.is_platform_admin
