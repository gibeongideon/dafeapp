import json
import logging

from django.conf import settings

from backups.models import OdooInstanceBackupSchedule

logger = logging.getLogger(__name__)


def backup_schedule_task_name(schedule_id: int) -> str:
    return f"backup-schedule-{schedule_id}"


def sync_backup_schedule_periodic_task(schedule: OdooInstanceBackupSchedule) -> None:
    try:
        from django_celery_beat.models import CrontabSchedule, PeriodicTask
    except Exception:
        logger.warning("django_celery_beat models unavailable; skipping backup schedule sync.", exc_info=True)
        return

    task_name = backup_schedule_task_name(schedule.pk)
    if not schedule.enabled:
        PeriodicTask.objects.filter(name=task_name).delete()
        return

    timezone_name = getattr(settings, "TIME_ZONE", "UTC") or "UTC"
    crontab_defaults = {
        "minute": str(schedule.minute_utc),
        "hour": str(schedule.hour_utc),
        "day_of_week": "*" if schedule.frequency == OdooInstanceBackupSchedule.Frequency.DAILY else str(schedule.weekday),
        "day_of_month": "*",
        "month_of_year": "*",
    }
    if "timezone" in {field.name for field in CrontabSchedule._meta.fields}:
        crontab_defaults["timezone"] = timezone_name
    crontab, _ = CrontabSchedule.objects.get_or_create(**crontab_defaults)

    PeriodicTask.objects.update_or_create(
        name=task_name,
        defaults={
            "task": "backups.tasks.run_scheduled_instance_backup",
            "crontab": crontab,
            "args": json.dumps([schedule.instance_id]),
            "enabled": True,
            "description": f"Scheduled backup for instance {schedule.instance.db_name} (schedule #{schedule.pk})",
        },
    )


def delete_backup_schedule_periodic_task(schedule_id: int) -> None:
    try:
        from django_celery_beat.models import PeriodicTask
    except Exception:
        logger.warning("django_celery_beat models unavailable; skipping backup schedule deletion.", exc_info=True)
        return
    PeriodicTask.objects.filter(name=backup_schedule_task_name(schedule_id)).delete()
