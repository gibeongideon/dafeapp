from django.apps import AppConfig


class BackupsConfig(AppConfig):
    name = "backups"

    def ready(self):
        from . import signals  # noqa: F401
