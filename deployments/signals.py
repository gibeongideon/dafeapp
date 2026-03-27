import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models.signals import post_delete
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
