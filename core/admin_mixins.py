from django.contrib import admin, messages
from django.db import models


PLATFORM_OWNER_ROLE = "OWNER"
PLATFORM_SUPPORT_ROLE = "SUPPORT"
PLATFORM_FINANCE_ROLE = "FINANCE"
PLATFORM_OPERATIONS_ROLE = "OPERATIONS"

ALL_PLATFORM_ROLES = {
    PLATFORM_OWNER_ROLE,
    PLATFORM_SUPPORT_ROLE,
    PLATFORM_FINANCE_ROLE,
    PLATFORM_OPERATIONS_ROLE,
}


def effective_platform_role(user):
    if not getattr(user, "is_authenticated", False):
        return ""
    if getattr(user, "is_superuser", False) or getattr(user, "is_platform_admin", False):
        return PLATFORM_OWNER_ROLE
    return getattr(user, "platform_role", "") or ""


def has_platform_role(user, *roles):
    role = effective_platform_role(user)
    return bool(role) and role in set(roles)


class RoleControlledAdminMixin(admin.ModelAdmin):
    view_roles = set()
    change_roles = set()
    add_roles = set()
    delete_roles = set()
    readonly_roles = set()

    def _can(self, request, allowed_roles):
        user = getattr(request, "user", None)
        if not user or not user.is_active:
            return False
        if getattr(user, "is_superuser", False) or getattr(user, "is_platform_admin", False):
            return True
        return has_platform_role(user, *allowed_roles)

    def has_module_permission(self, request):
        return self._can(request, self.view_roles)

    def has_view_permission(self, request, obj=None):
        return self._can(request, self.view_roles)

    def has_change_permission(self, request, obj=None):
        return self._can(request, self.change_roles)

    def has_add_permission(self, request):
        return self._can(request, self.add_roles)

    def has_delete_permission(self, request, obj=None):
        return self._can(request, self.delete_roles)

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if (
            not getattr(request.user, "is_superuser", False)
            and not getattr(request.user, "is_platform_admin", False)
            and has_platform_role(request.user, *self.readonly_roles)
        ):
            for field in self.model._meta.get_fields():
                if isinstance(field, (models.Field, models.ManyToManyField)):
                    readonly.append(field.name)
        return tuple(dict.fromkeys(readonly))

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not self.has_change_permission(request):
            return {}
        return actions

    def deny_action_for_readonly_roles(self, request, queryset, *, role_name="selected records"):
        self.message_user(
            request,
            f"Your platform role is read-only for {role_name}.",
            level=messages.WARNING,
        )


class ReadOnlyAdminMixin(RoleControlledAdminMixin):
    change_roles = set()
    add_roles = set()
    delete_roles = set()

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
