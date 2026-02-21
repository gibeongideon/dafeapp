from django.apps import AppConfig


class OrganizationsConfig(AppConfig):
    name = "organizations"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        import organizations.signals  # noqa: F401
