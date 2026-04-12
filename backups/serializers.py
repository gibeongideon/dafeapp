from rest_framework import serializers

from backups.models import OdooInstanceBackup, OdooInstanceBackupSchedule


class OdooInstanceBackupSerializer(serializers.ModelSerializer):
    size_display = serializers.CharField(read_only=True)
    created_by_email = serializers.SerializerMethodField()

    class Meta:
        model = OdooInstanceBackup
        fields = [
            "id",
            "backup_type",
            "status",
            "backup_dir",
            "db_backup_path",
            "filestore_backup_path",
            "size_bytes",
            "size_display",
            "log",
            "note",
            "created_by_email",
            "created_at",
            "updated_at",
        ]

    def get_created_by_email(self, obj):
        return obj.created_by.email if obj.created_by_id else None


class OdooInstanceBackupScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = OdooInstanceBackupSchedule
        fields = [
            "enabled",
            "frequency",
            "weekday",
            "hour_utc",
            "minute_utc",
            "created_at",
            "updated_at",
        ]
