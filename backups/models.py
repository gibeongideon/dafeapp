from django.conf import settings
from django.db import models


class OdooInstanceBackup(models.Model):
    """A point-in-time backup of an OdooInstance: database dump + filestore archive."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        DONE    = "DONE",    "Done"
        FAILED  = "FAILED",  "Failed"

    class BackupType(models.TextChoices):
        FULL    = "FULL",    "Full (DB + Filestore)"
        DB_ONLY = "DB_ONLY", "Database only"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="instance_backups",
    )
    instance = models.ForeignKey(
        "deployments.OdooInstance",
        on_delete=models.CASCADE,
        related_name="backups",
    )
    backup_type = models.CharField(
        max_length=10,
        choices=BackupType.choices,
        default=BackupType.FULL,
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
    # Paths on the remote server
    backup_dir            = models.CharField(max_length=500, blank=True, default="")
    db_backup_path        = models.CharField(max_length=500, blank=True, default="")
    filestore_backup_path = models.CharField(max_length=500, blank=True, default="")
    # Total size of the backup directory in bytes (populated on completion)
    size_bytes = models.BigIntegerField(default=0)
    # Task execution log
    log = models.TextField(blank=True)
    # Optional human-readable label set by the user
    note = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="instance_backups",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["instance", "-created_at"]),
            models.Index(fields=["organization", "-created_at"]),
        ]

    def __str__(self):
        return f"Backup #{self.pk} [{self.instance.db_name}] {self.status}"

    @property
    def size_display(self) -> str:
        b = self.size_bytes or 0
        if b >= 1_073_741_824:
            return f"{b / 1_073_741_824:.1f} GB"
        if b >= 1_048_576:
            return f"{b / 1_048_576:.1f} MB"
        if b >= 1024:
            return f"{b / 1024:.0f} KB"
        return f"{b} B"


class OdooInstanceBackupSchedule(models.Model):
    class Frequency(models.TextChoices):
        DAILY = "DAILY", "Daily"
        WEEKLY = "WEEKLY", "Weekly"

    class Weekday(models.TextChoices):
        MONDAY = "1", "Monday"
        TUESDAY = "2", "Tuesday"
        WEDNESDAY = "3", "Wednesday"
        THURSDAY = "4", "Thursday"
        FRIDAY = "5", "Friday"
        SATURDAY = "6", "Saturday"
        SUNDAY = "0", "Sunday"

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="instance_backup_schedules",
    )
    instance = models.ForeignKey(
        "deployments.OdooInstance",
        on_delete=models.CASCADE,
        related_name="backup_schedules",
    )
    enabled = models.BooleanField(default=False)
    frequency = models.CharField(
        max_length=10,
        choices=Frequency.choices,
        default=Frequency.DAILY,
    )
    weekday = models.CharField(
        max_length=1,
        choices=Weekday.choices,
        default=Weekday.SUNDAY,
    )
    hour_utc = models.PositiveSmallIntegerField(default=2)
    minute_utc = models.PositiveSmallIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_instance_backup_schedules",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="updated_instance_backup_schedules",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["instance_id"]
        indexes = [
            models.Index(fields=["organization", "enabled"]),
        ]

    def __str__(self):
        state = "enabled" if self.enabled else "disabled"
        return f"Backup schedule for {self.instance.db_name} ({state})"
