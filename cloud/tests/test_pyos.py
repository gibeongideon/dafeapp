"""
PyOS service tests — 4 cases, paramiko is fully mocked.
"""

import socket
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

import paramiko

from cloud.encryption import FieldEncryptor
from cloud.models import ExternalServer, PyOSSSHSettings
from cloud.pyos import PyOSService, resolve_private_key_string
from organizations.models import Organization

User = get_user_model()

KEY = "HhC9AeGmYdlCNhCQ3JkHgSnMRFZLYpbMJb7SLxHRi1g="


def _make_server(org):
    server = ExternalServer(
        organization=org,
        name="test-server",
        host="192.168.1.100",
        port=22,
        username="root",
        auth_type=ExternalServer.AuthType.PASSWORD,
    )
    # Set encrypted password directly so we skip model save encryption flow
    server.encrypted_password = FieldEncryptor.encrypt("testpass")
    server.pk = 1  # simulate saved instance
    return server


@override_settings(FIELD_ENCRYPTION_KEY=KEY)
class PyOSServiceTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="pyos@test.com", password="pass")
        cls.org = Organization.objects.create(name="PyOS Org", owner=cls.user)

    @patch("paramiko.SSHClient")
    def test_validate_success(self, mock_ssh_cls):
        """validate() → SSH echo OK → is_verified=True."""
        mock_client = MagicMock()
        mock_ssh_cls.return_value = mock_client

        # exec_command returns (stdin, stdout, stderr) mocks
        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b"OK\n"
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b""
        mock_client.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)

        server = _make_server(self.org)
        service = PyOSService(server)
        success, msg = service.validate()

        self.assertTrue(success)
        self.assertIn("successful", msg)

    @patch("paramiko.SSHClient")
    def test_validate_auth_failure(self, mock_ssh_cls):
        """validate() → AuthenticationException → is_verified=False."""
        mock_client = MagicMock()
        mock_ssh_cls.return_value = mock_client
        mock_client.connect.side_effect = paramiko.AuthenticationException("Bad credentials")

        server = _make_server(self.org)
        service = PyOSService(server)
        success, msg = service.validate()

        self.assertFalse(success)
        self.assertIn("Authentication", msg)

    @patch("paramiko.SSHClient")
    def test_validate_host_unreachable(self, mock_ssh_cls):
        """validate() → socket.timeout → is_verified=False."""
        mock_client = MagicMock()
        mock_ssh_cls.return_value = mock_client
        mock_client.connect.side_effect = socket.timeout("Connection timed out")

        server = _make_server(self.org)
        service = PyOSService(server)
        success, msg = service.validate()

        self.assertFalse(success)
        self.assertIn("unreachable", msg)

    @patch("paramiko.SSHClient")
    def test_prepare_server_runs_all_commands(self, mock_ssh_cls):
        """prepare_server() calls exec_command for each PREPARE_COMMAND."""
        from cloud.pyos import PREPARE_COMMANDS

        mock_client = MagicMock()
        mock_ssh_cls.return_value = mock_client

        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b""
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b""
        mock_client.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)

        server = _make_server(self.org)
        service = PyOSService(server)
        success, log = service.prepare_server()

        self.assertTrue(success)
        self.assertEqual(mock_client.exec_command.call_count, len(PREPARE_COMMANDS))
        # Verify each command was issued
        issued_cmds = [call.args[0] for call in mock_client.exec_command.call_args_list]
        self.assertEqual(issued_cmds, PREPARE_COMMANDS)

    def test_resolve_private_key_string_uses_default_settings_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / "id_ed25519"
            key_file.write_text("dummy-private-key")
            settings_obj = PyOSSSHSettings.get_or_create_settings()
            settings_obj.default_ssh_key_path = str(key_file)
            settings_obj.save()

            server = _make_server(self.org)
            server.ssh_key_path = ""
            private_key_str, source = resolve_private_key_string(server)

            self.assertEqual(private_key_str, "dummy-private-key")
            self.assertEqual(source, str(key_file))
