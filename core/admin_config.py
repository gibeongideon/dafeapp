from django.contrib.admin.apps import AdminConfig


class PlatformAdminConfig(AdminConfig):
    default_site = "core.admin_site.PlatformAdminSite"
