import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db.models.signals import post_delete, post_migrate
from django.dispatch import receiver

from deployments.models import OdooServer

logger = logging.getLogger(__name__)


def _broadcast_server_event(server_id: int, payload: dict):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            f"odoo.server.{server_id}",
            {"type": "server.update", "payload": payload},
        )
    except Exception:
        logger.warning("Server broadcast skipped for server %s", server_id, exc_info=True)


@receiver(post_delete, sender=OdooServer)
def odoo_server_deleted(sender, instance: OdooServer, **kwargs):
    server_id = getattr(instance, "pk", None)
    if server_id is None:
        return
    _broadcast_server_event(server_id, {"type": "removed", "server_id": server_id, "reason": "deleted"})


def _sync_connectivity_periodic_task():
    """
    Keep the DB-backed celery beat task aligned with the code/config default.
    This matters because DatabaseScheduler keeps its own copy in the database.
    """
    try:
        from django_celery_beat.models import IntervalSchedule, PeriodicTask
    except Exception:
        logger.warning("django_celery_beat models unavailable; skipping connectivity schedule sync.", exc_info=True)
        return

    interval_seconds = max(1, int(getattr(settings, "CELERY_SERVER_CONNECTIVITY_INTERVAL_SECONDS", 180)))
    _sync_interval_periodic_task(
        name="check-server-connectivity",
        task="deployments.tasks.check_server_connectivity",
        interval_seconds=interval_seconds,
    )
    logger.info(
        "Synced celery beat task 'check-server-connectivity' to every %s second(s).",
        interval_seconds,
    )


def _sync_interval_periodic_task(*, name: str, task: str, interval_seconds: int):
    from django_celery_beat.models import IntervalSchedule, PeriodicTask

    if interval_seconds >= 3600 and interval_seconds % 3600 == 0:
        every, period = interval_seconds // 3600, IntervalSchedule.HOURS
    elif interval_seconds >= 60 and interval_seconds % 60 == 0:
        every, period = interval_seconds // 60, IntervalSchedule.MINUTES
    else:
        every, period = interval_seconds, IntervalSchedule.SECONDS
    interval, _ = IntervalSchedule.objects.get_or_create(every=every, period=period)
    PeriodicTask.objects.update_or_create(
        name=name,
        defaults={
            "task": task,
            "interval": interval,
            "enabled": True,
        },
    )


def _sync_heartbeat_periodic_tasks():
    try:
        from django_celery_beat.models import IntervalSchedule  # noqa: F401
    except Exception:
        logger.warning("django_celery_beat models unavailable; skipping heartbeat schedule sync.", exc_info=True)
        return

    heartbeat_interval = max(1, int(getattr(settings, "CELERY_SERVER_HEARTBEAT_INTERVAL_SECONDS", 60)))
    repair_interval = max(60, int(getattr(settings, "CELERY_SERVER_HEARTBEAT_REPAIR_INTERVAL_SECONDS", 3600)))
    _sync_interval_periodic_task(
        name="mark-disconnected-servers",
        task="deployments.tasks.mark_disconnected_servers",
        interval_seconds=heartbeat_interval,
    )
    _sync_interval_periodic_task(
        name="repair-stale-heartbeat-agents",
        task="deployments.tasks.repair_stale_heartbeat_agents",
        interval_seconds=repair_interval,
    )
    logger.info(
        "Synced heartbeat beat tasks: disconnect every %s second(s), repair every %s second(s).",
        heartbeat_interval,
        repair_interval,
    )


@receiver(post_migrate)
def sync_deployment_periodic_tasks(sender, **kwargs):
    if getattr(sender, "label", "") != "deployments":
        return
    _sync_connectivity_periodic_task()
    _sync_heartbeat_periodic_tasks()
