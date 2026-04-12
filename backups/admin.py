from django.contrib import admin

from backups.models import OdooInstanceBackup


@admin.register(OdooInstanceBackup)
class OdooInstanceBackupAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "instance",
        "backup_type",
        "status",
        "size_display",
        "note",
        "created_by",
        "created_at",
    )
    list_filter = ("status", "backup_type")
    search_fields = ("instance__db_name", "instance__name", "note")
    readonly_fields = (
        "backup_dir",
        "db_backup_path",
        "filestore_backup_path",
        "size_bytes",
        "size_display",
        "log",
        "created_at",
        "updated_at",
    )
    ordering = ("-created_at",)
