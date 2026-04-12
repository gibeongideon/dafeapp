import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from backups.models import OdooInstanceBackup
from deployments.models import OdooInstance, OdooServer
from organizations.models import Organization, OrganizationMembership

User = get_user_model()


class _FakeChannel:
    def __init__(self, exit_status=0):
        self._exit_status = exit_status
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv_exit_status(self):
        return self._exit_status


class _FakeStream:
    def __init__(self, payload=b"", exit_status=0):
        self._buffer = io.BytesIO(payload)
        self.channel = _FakeChannel(exit_status=exit_status)

    def read(self, size=-1):
        return self._buffer.read(size)

    def close(self):
        return None


class _FakeClient:
    def __init__(self, payload=b""):
        self.payload = payload
        self.closed = False
        self.command = None
        self.timeout = None

    def exec_command(self, command, timeout=None):
        self.command = command
        self.timeout = timeout
        return (
            _FakeStream(),
            _FakeStream(self.payload, exit_status=0),
            _FakeStream(),
        )

    def close(self):
        self.closed = True


class BackupDownloadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="backup@test.com", password="pass")
        cls.org = Organization.objects.create(name="Backup Org", owner=cls.user)
        OrganizationMembership.objects.create(
            user=cls.user,
            organization=cls.org,
            role=OrganizationMembership.Role.SUPER_ADMIN,
        )
        cls.server = OdooServer.objects.create(
            organization=cls.org,
            name="odoo19-server",
            odoo_version=OdooServer.OdooVersion.V19,
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            deployment_mode=OdooServer.DeploymentMode.DOCKER,
        )
        cls.instance = OdooInstance.objects.create(
            organization=cls.org,
            server=cls.server,
            name="Production",
            db_name="prod_db",
            http_port=8069,
            status=OdooInstance.Status.RUNNING,
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_org_id"] = self.org.id
        session.save()

    @patch("backups.views._connect_ssh_client")
    @patch("backups.views._ssh_run")
    def test_download_backup_streams_archive_attachment(self, mock_ssh_run, mock_connect_ssh_client):
        backup = OdooInstanceBackup.objects.create(
            organization=self.org,
            instance=self.instance,
            backup_type=OdooInstanceBackup.BackupType.FULL,
            status=OdooInstanceBackup.Status.DONE,
            backup_dir="/backups/dafeapp/prod_db/20260412_191400",
            db_backup_path="/backups/dafeapp/prod_db/20260412_191400/db.sql.gz",
            filestore_backup_path="/backups/dafeapp/prod_db/20260412_191400/filestore.tar.gz",
            size_bytes=1024,
            created_by=self.user,
        )
        fake_client = _FakeClient(payload=b"backup-archive-bytes")
        mock_ssh_run.return_value = (0, "")
        mock_connect_ssh_client.return_value = (fake_client, None)

        response = self.client.get(
            reverse(
                "backups:instance-backup-download",
                kwargs={"instance_id": self.instance.id, "backup_id": backup.id},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/gzip")
        self.assertIn('attachment; filename="prod_db_backup_', response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"backup-archive-bytes")
        self.assertTrue(fake_client.closed)
        self.assertIn("tar -C /backups/dafeapp/prod_db -czf - 20260412_191400", fake_client.command)
