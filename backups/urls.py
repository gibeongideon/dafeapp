from django.urls import path

from backups import views

app_name = "backups"

urlpatterns = [
    path(
        "instances/<int:instance_id>/",
        views.InstanceBackupListAPIView.as_view(),
        name="instance-backup-list",
    ),
    path(
        "instances/<int:instance_id>/backup/",
        views.CreateBackupAPIView.as_view(),
        name="instance-backup-create",
    ),
    path(
        "instances/<int:instance_id>/schedule/",
        views.InstanceBackupScheduleAPIView.as_view(),
        name="instance-backup-schedule",
    ),
    path(
        "instances/<int:instance_id>/schedule/<int:schedule_id>/",
        views.BackupScheduleDetailAPIView.as_view(),
        name="instance-backup-schedule-detail",
    ),
    path(
        "instances/<int:instance_id>/download/<int:backup_id>/",
        views.DownloadBackupAPIView.as_view(),
        name="instance-backup-download",
    ),
    path(
        "instances/<int:instance_id>/restore/<int:backup_id>/",
        views.RestoreBackupAPIView.as_view(),
        name="instance-backup-restore",
    ),
    path(
        "instances/<int:instance_id>/restore/upload/",
        views.UploadRestoreBackupAPIView.as_view(),
        name="instance-backup-restore-upload",
    ),
    path(
        "instances/<int:instance_id>/restore-to-new/<int:backup_id>/",
        views.RestoreToNewInstanceAPIView.as_view(),
        name="instance-backup-restore-to-new",
    ),
]
