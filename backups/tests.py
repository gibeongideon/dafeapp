import io
import zipfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django_celery_beat.models import CrontabSchedule, PeriodicTask

from backups.models import OdooInstanceBackup, OdooInstanceBackupSchedule
from backups.scheduling import backup_schedule_task_name, sync_backup_schedule_periodic_task
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
        self.sftp = None

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

    def open_sftp(self):
        self.sftp = _FakeSFTP()
        return self.sftp


class _FakeSFTP:
    def __init__(self):
        self.put_calls = []
        self.closed = False

    def put(self, local_path, remote_path):
        self.put_calls.append((local_path, remote_path))

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
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertIn('attachment; filename="prod_db_backup_', response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"backup-archive-bytes")
        self.assertTrue(fake_client.closed)
        self.assertIn("python3 -c", fake_client.command)
        self.assertIn("/backups/dafeapp/prod_db/20260412_191400", fake_client.command)

    @patch("backups.views._dispatch")
    @patch("backups.views._connect_ssh_client")
    @patch("backups.views._ssh_run")
    def test_upload_restore_zip_creates_backup_and_dispatches_restore(self, mock_ssh_run, mock_connect_ssh_client, mock_dispatch):
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("20260412_191400/db.sql.gz", b"fake-db")
            archive.writestr("20260412_191400/filestore.tar.gz", b"fake-filestore")

        upload = SimpleUploadedFile(
            "prod_backup.zip",
            archive_buffer.getvalue(),
            content_type="application/zip",
        )
        fake_client = _FakeClient()
        mock_connect_ssh_client.return_value = (fake_client, None)
        mock_ssh_run.return_value = (0, "")

        response = self.client.post(
            reverse(
                "backups:instance-backup-restore-upload",
                kwargs={"instance_id": self.instance.id},
            ),
            {"archive": upload},
        )

        self.assertEqual(response.status_code, 202)
        created_backup = OdooInstanceBackup.objects.latest("id")
        self.assertEqual(created_backup.status, OdooInstanceBackup.Status.DONE)
        self.assertTrue(created_backup.backup_dir.endswith("/contents/20260412_191400"))
        self.assertTrue(created_backup.db_backup_path.endswith("/contents/20260412_191400/db.sql.gz"))
        self.assertTrue(created_backup.filestore_backup_path.endswith("/contents/20260412_191400/filestore.tar.gz"))
        self.assertIn("Uploaded ZIP restore", created_backup.note)

        self.assertTrue(fake_client.closed)
        self.assertIsNotNone(fake_client.sftp)
        self.assertEqual(len(fake_client.sftp.put_calls), 1)
        self.assertTrue(fake_client.sftp.put_calls[0][1].endswith("/upload.zip"))

        commands = [call.args[1] for call in mock_ssh_run.call_args_list]
        self.assertTrue(any(command.startswith("mkdir -p ") and "/backups/dafeapp/.uploaded/prod_db/" in command for command in commands))
        self.assertTrue(any("python3 -c" in command and "upload.zip" in command for command in commands))
        self.assertTrue(any(command.startswith("rm -f ") and "/upload.zip" in command for command in commands))

        dispatch_args = mock_dispatch.call_args[0]
        self.assertEqual(dispatch_args[0].__name__, "restore_odoo_instance")
        self.assertEqual(dispatch_args[1], self.instance.pk)
        self.assertEqual(dispatch_args[2], created_backup.pk)


class BackupScheduleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="schedule@test.com", password="pass")
        cls.org = Organization.objects.create(name="Schedule Org", owner=cls.user)
        OrganizationMembership.objects.create(
            user=cls.user,
            organization=cls.org,
            role=OrganizationMembership.Role.SUPER_ADMIN,
        )
        cls.server = OdooServer.objects.create(
            organization=cls.org,
            name="odoo19-schedule-server",
            odoo_version=OdooServer.OdooVersion.V19,
            region="nyc3",
            size="s-2vcpu-4gb",
            status=OdooServer.Status.PROVISIONED,
            deployment_mode=OdooServer.DeploymentMode.DOCKER,
        )
        cls.instance = OdooInstance.objects.create(
            organization=cls.org,
            server=cls.server,
            name="Scheduled Instance",
            db_name="scheduled_db",
            http_port=8070,
            status=OdooInstance.Status.RUNNING,
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_org_id"] = self.org.id
        session.save()

    def test_schedule_api_returns_defaults_when_missing(self):
        response = self.client.get(
            reverse("backups:instance-backup-schedule", kwargs={"instance_id": self.instance.id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {
                "enabled": False,
                "frequency": "DAILY",
                "weekday": "0",
                "hour_utc": 2,
                "minute_utc": 0,
            },
        )

    def test_schedule_api_saves_schedule(self):
        response = self.client.post(
            reverse("backups:instance-backup-schedule", kwargs={"instance_id": self.instance.id}),
            data='{"enabled": true, "frequency": "WEEKLY", "weekday": "5", "hour_utc": 3, "minute_utc": 45}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        schedule = OdooInstanceBackupSchedule.objects.get(instance=self.instance)
        self.assertTrue(schedule.enabled)
        self.assertEqual(schedule.frequency, OdooInstanceBackupSchedule.Frequency.WEEKLY)
        self.assertEqual(schedule.weekday, OdooInstanceBackupSchedule.Weekday.FRIDAY)
        self.assertEqual(schedule.hour_utc, 3)
        self.assertEqual(schedule.minute_utc, 45)
        self.assertEqual(schedule.created_by, self.user)
        self.assertEqual(schedule.updated_by, self.user)

    def test_sync_schedule_creates_periodic_task(self):
        schedule = OdooInstanceBackupSchedule.objects.create(
            organization=self.org,
            instance=self.instance,
            enabled=True,
            frequency=OdooInstanceBackupSchedule.Frequency.WEEKLY,
            weekday=OdooInstanceBackupSchedule.Weekday.MONDAY,
            hour_utc=4,
            minute_utc=30,
            created_by=self.user,
        )

        sync_backup_schedule_periodic_task(schedule)

        periodic_task = PeriodicTask.objects.get(name=backup_schedule_task_name(self.instance.id))
        self.assertEqual(periodic_task.task, "backups.tasks.run_scheduled_instance_backup")
        self.assertTrue(periodic_task.enabled)
        self.assertEqual(periodic_task.args, f"[{self.instance.id}]")
        self.assertIsInstance(periodic_task.crontab, CrontabSchedule)
        self.assertEqual(periodic_task.crontab.minute, "30")
        self.assertEqual(periodic_task.crontab.hour, "4")
        self.assertEqual(periodic_task.crontab.day_of_week, "1")
