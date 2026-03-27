from django.apps import AppConfig


class DeploymentsConfig(AppConfig):
    name = "deployments"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        import deployments.signals  # noqa: F401
