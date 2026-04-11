from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from core.admin_mixins import (
    PLATFORM_OWNER_ROLE,
    PLATFORM_SUPPORT_ROLE,
    RoleControlledAdminMixin,
)
from .models import User


class MembershipInline(admin.TabularInline):
    from organizations.models import OrganizationMembership
    model = OrganizationMembership
    fk_name = "user"   # OrganizationMembership has two FKs to User
    extra = 0
    fields = ("organization", "role", "is_active", "joined_at")
    readonly_fields = ("joined_at",)


@admin.register(User)
class UserAdmin(RoleControlledAdminMixin, BaseUserAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

    list_display = (
        "email", "get_full_name", "effective_platform_role_display", "is_platform_admin",
        "is_email_verified", "login_count", "is_active", "date_joined",
    )
    list_filter = ("is_platform_admin", "platform_role", "is_email_verified", "is_staff", "is_active")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("-date_joined",)

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal Info", {"fields": ("first_name", "last_name", "username")}),
        ("Platform Access", {"fields": ("is_platform_admin", "platform_role", "is_email_verified", "is_active", "is_staff", "is_superuser")}),
        ("Login Tracking", {"fields": ("last_login_ip", "login_count", "last_login")}),
        ("Permissions", {"fields": ("groups", "user_permissions")}),
        ("Dates", {"fields": ("date_joined",)}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "password1", "password2", "is_platform_admin", "platform_role", "is_staff"),
        }),
    )
    readonly_fields = ("last_login_ip", "login_count", "last_login", "date_joined")
    inlines = [MembershipInline]

    @admin.display(description="Platform Role")
    def effective_platform_role_display(self, obj):
        return obj.effective_platform_role or "—"
