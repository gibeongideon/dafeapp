import logging

from django.db.models.signals import post_delete, post_migrate, post_save
from django.dispatch import receiver

from backups.models import OdooInstanceBackupSchedule
from backups.scheduling import delete_backup_schedule_periodic_task, sync_backup_schedule_periodic_task

logger = logging.getLogger(__name__)


@receiver(post_save, sender=OdooInstanceBackupSchedule)
def sync_instance_backup_schedule(sender, instance: OdooInstanceBackupSchedule, **kwargs):
    sync_backup_schedule_periodic_task(instance)


@receiver(post_delete, sender=OdooInstanceBackupSchedule)
def delete_instance_backup_schedule(sender, instance: OdooInstanceBackupSchedule, **kwargs):
    delete_backup_schedule_periodic_task(instance.instance_id)


@receiver(post_migrate)
def sync_backup_periodic_tasks(sender, **kwargs):
    if getattr(sender, "label", "") != "backups":
        return
    for schedule in OdooInstanceBackupSchedule.objects.select_related("instance").all():
        try:
            sync_backup_schedule_periodic_task(schedule)
        except Exception:
            logger.warning("Could not sync backup schedule for instance %s", schedule.instance_id, exc_info=True)
