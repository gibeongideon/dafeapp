from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = (
        "email", "get_full_name", "role", "is_email_verified",
        "login_count", "last_login_ip", "is_active", "date_joined",
    )
    list_filter = ("role", "is_email_verified", "is_staff", "is_active")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("-date_joined",)

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal Info", {"fields": ("first_name", "last_name", "username")}),
        ("Role & Status", {"fields": ("role", "is_email_verified", "is_active")}),
        ("Login Tracking", {"fields": ("last_login_ip", "login_count", "last_login")}),
        ("Permissions", {"fields": ("is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("date_joined",)}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "password1", "password2", "role"),
        }),
    )
    readonly_fields = ("last_login_ip", "login_count", "last_login", "date_joined")
