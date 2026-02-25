"""
Celery tasks for asynchronous cloud operations.
"""

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def validate_external_server(self, server_id: int):
    """SSH-validate a PYOS server and persist the result."""
    from audit.models import AuditLog
    from cloud.models import ExternalServer
    from cloud.pyos import PyOSService
    from core.utils import log_audit

    try:
        server = ExternalServer.objects.get(pk=server_id)
    except ExternalServer.DoesNotExist:
        logger.error("ExternalServer %s not found", server_id)
        return

    service = PyOSService(server)
    success, message = service.validate()

    server.is_verified = success
    server.verification_error = "" if success else message
    server.last_verified_at = timezone.now()
    server.save(update_fields=["is_verified", "verification_error", "last_verified_at"])

    action = AuditLog.Action.SERVER_VERIFY
    log_audit(None, action, None, f"Server '{server.name}': {message}", organization=server.organization)


@shared_task(bind=True, max_retries=3)
def prepare_external_server(self, server_id: int):
    """Run Docker/UFW preparation commands on a PYOS server."""
    from audit.models import AuditLog
    from cloud.models import ExternalServer
    from cloud.pyos import PyOSService
    from core.utils import log_audit

    try:
        server = ExternalServer.objects.get(pk=server_id)
    except ExternalServer.DoesNotExist:
        logger.error("ExternalServer %s not found", server_id)
        return

    from cloud.models import ExternalServer as ES
    server.preparation_status = ES.PreparationStatus.IN_PROGRESS
    server.save(update_fields=["preparation_status"])

    service = PyOSService(server)
    success, log_output = service.prepare_server()

    server.preparation_log = log_output
    server.preparation_status = (
        ES.PreparationStatus.DONE if success else ES.PreparationStatus.FAILED
    )
    server.is_prepared = success
    server.save(update_fields=["preparation_log", "preparation_status", "is_prepared"])

    result = "Preparation complete." if success else f"Preparation failed: {log_output[-200:]}"
    log_audit(None, AuditLog.Action.SERVER_PREPARE, None, f"Server '{server.name}': {result}", organization=server.organization)


@shared_task(bind=True, max_retries=3)
def validate_cloud_account(self, account_id: int):
    """Verify a DigitalOcean API token and persist the result."""
    from audit.models import AuditLog
    from cloud.models import CloudAccount
    from cloud.providers import get_provider
    from core.utils import log_audit

    try:
        account = CloudAccount.objects.get(pk=account_id)
    except CloudAccount.DoesNotExist:
        logger.error("CloudAccount %s not found", account_id)
        return

    provider = get_provider(account)
    success, message = provider.validate_credentials()

    account.is_verified = success
    account.verification_error = "" if success else message
    account.last_verified_at = timezone.now()
    account.save(update_fields=["is_verified", "verification_error", "last_verified_at"])

    log_audit(None, AuditLog.Action.CLOUD_ACCT_VERIFY, None, f"Account '{account.name}': {message}", organization=account.organization)


@shared_task(bind=True, max_retries=3)
def provision_do_server(self, cloud_server_id: int):
    """
    Provision a managed cloud server, apply firewall, poll until active/running.
    """
    import time

    from audit.models import AuditLog
    from cloud.models import CloudServer
    from cloud.providers import get_provider
    from core.utils import log_audit

    try:
        cloud_server = CloudServer.objects.get(pk=cloud_server_id)
    except CloudServer.DoesNotExist:
        logger.error("CloudServer %s not found", cloud_server_id)
        return

    cloud_server.status = CloudServer.Status.PROVISIONING
    cloud_server.save(update_fields=["status"])

    provider = get_provider(cloud_server.cloud_account)

    try:
        droplet = provider.create_server(
            name=cloud_server.name,
            region=cloud_server.region,
            size=cloud_server.size,
        )
        droplet_id = str(droplet["id"])
        cloud_server.provider_server_id = droplet_id
        cloud_server.save(update_fields=["provider_server_id"])

        # Apply firewall
        provider.create_firewall(droplet_id)

        # Poll for running status (max 5 min)
        for _ in range(30):
            time.sleep(10)
            status = provider.get_server_status(droplet_id)
            if status in ("active", "running"):
                cloud_server.ip_address = provider.get_server_ip(droplet_id) or None
                cloud_server.status = CloudServer.Status.RUNNING
                cloud_server.save(update_fields=["status", "ip_address", "updated_at"])

                # Create Infrastructure record
                from cloud.models import Infrastructure
                Infrastructure.objects.get_or_create(
                    cloud_server=cloud_server,
                    defaults={
                        "organization": cloud_server.organization,
                        "infra_type": Infrastructure.InfraType.MANAGED,
                        "name": cloud_server.name,
                        "is_ready": True,
                    },
                )
                log_audit(None, AuditLog.Action.DROPLET_PROVISION, None,
                          f"Droplet '{cloud_server.name}' provisioned ({droplet_id})",
                          organization=cloud_server.organization)
                return

        # Timed out
        cloud_server.status = CloudServer.Status.FAILED
        cloud_server.save(update_fields=["status"])
        log_audit(None, AuditLog.Action.DROPLET_PROVISION, None,
                  f"Droplet '{cloud_server.name}' provisioning timed out.",
                  organization=cloud_server.organization)

    except Exception as exc:
        cloud_server.status = CloudServer.Status.FAILED
        cloud_server.save(update_fields=["status"])
        logger.exception("Droplet provisioning failed for CloudServer %s", cloud_server_id)
        raise self.retry(exc=exc, countdown=30)
