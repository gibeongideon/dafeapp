from django.contrib import admin

from core.admin_mixins import PLATFORM_OWNER_ROLE, PLATFORM_SUPPORT_ROLE, RoleControlledAdminMixin
from .models import Organization, OrganizationInvite, OrganizationMembership


class MembershipInline(admin.TabularInline):
    model = OrganizationMembership
    extra = 0
    fields = ("user", "role", "is_active", "joined_at")
    readonly_fields = ("joined_at",)


@admin.register(Organization)
class OrganizationAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

    list_display = ("name", "slug", "owner", "member_count", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "owner__email")
    readonly_fields = ("slug", "created_at", "updated_at")
    inlines = [MembershipInline]


@admin.register(OrganizationMembership)
class MembershipAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

    list_display = ("user", "organization", "role", "is_active", "joined_at")
    list_filter = ("role", "is_active", "organization")
    search_fields = ("user__email", "organization__name")


@admin.register(OrganizationInvite)
class InviteAdmin(RoleControlledAdminMixin, admin.ModelAdmin):
    view_roles = {PLATFORM_OWNER_ROLE, PLATFORM_SUPPORT_ROLE}
    change_roles = {PLATFORM_OWNER_ROLE}
    add_roles = {PLATFORM_OWNER_ROLE}
    delete_roles = {PLATFORM_OWNER_ROLE}
    readonly_roles = {PLATFORM_SUPPORT_ROLE}

    list_display = ("email", "organization", "role", "is_used", "is_expired", "created_at")
    list_filter = ("is_used", "organization")
    search_fields = ("email", "organization__name")
    readonly_fields = ("token", "created_at", "expires_at")

    def is_expired(self, obj):
        return obj.is_expired
    is_expired.boolean = True
