"""
PyOS service — validates and prepares a user-supplied VPS via SSH (paramiko).
"""

import logging
import socket

import paramiko

logger = logging.getLogger(__name__)

# Commands run sequentially during server preparation (bare-metal, no Docker)
PREPARE_COMMANDS = [
    "apt-get update -qq",
    "apt-get install -y -qq python3 python3-pip curl wget gnupg2 ufw",
    "ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw allow 8069 && ufw --force enable",
    "mkdir -p /opt/dafeapp/deployments",
]


class PyOSService:
    """SSH-based operations against a user-supplied VPS (ExternalServer)."""

    def __init__(self, server):
        self.server = server

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(self) -> tuple[bool, str]:
        """
        Open an SSH connection, run `echo OK`, close.
        Returns (success, message).
        """
        client = None
        try:
            client = self._get_client()
            stdout, stderr = self._run(client, "echo OK")
            if stdout.strip() == "OK":
                return True, "SSH connection successful."
            return False, f"Unexpected output: {stdout!r}"
        except paramiko.AuthenticationException:
            return False, "Authentication failed — check username and credentials."
        except (socket.timeout, paramiko.ssh_exception.NoValidConnectionsError) as exc:
            return False, f"Host unreachable: {exc}"
        except Exception as exc:
            logger.exception("SSH validate error for server %s", self.server.pk)
            return False, f"Error: {exc}"
        finally:
            if client:
                client.close()

    def prepare_server(self) -> tuple[bool, str]:
        """
        Run all PREPARE_COMMANDS sequentially via SSH.
        Returns (success, log_output).
        """
        client = None
        log_lines = []
        try:
            client = self._get_client()
            for cmd in PREPARE_COMMANDS:
                log_lines.append(f"$ {cmd}")
                stdout, stderr = self._run(client, cmd)
                if stdout:
                    log_lines.append(stdout.rstrip())
                if stderr:
                    log_lines.append(f"[stderr] {stderr.rstrip()}")
            return True, "\n".join(log_lines)
        except paramiko.AuthenticationException:
            msg = "Authentication failed during preparation."
            log_lines.append(msg)
            return False, "\n".join(log_lines)
        except Exception as exc:
            msg = f"Preparation error: {exc}"
            log_lines.append(msg)
            logger.exception("SSH prepare error for server %s", self.server.pk)
            return False, "\n".join(log_lines)
        finally:
            if client:
                client.close()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> paramiko.SSHClient:
        """
        Build and return a connected SSHClient.

        Uses paramiko.Transport directly so the SSH agent is never consulted —
        SSHClient.connect() can trigger agent auth even with allow_agent=False
        in paramiko 4.x when SSH_AUTH_SOCK is present in the environment.
        """
        import io

        sock = socket.create_connection(
            (self.server.host, self.server.port), timeout=15
        )
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=15)
        except Exception:
            transport.close()
            raise

        username = self.server.username

        if self.server.auth_type == "DAFEAPP_KEY":
            from cloud.models import SystemSSHKey
            system_key = SystemSSHKey.get_or_create_keypair()
            private_key_str = system_key.get_private_key()
            if not private_key_str:
                transport.close()
                raise paramiko.ssh_exception.SSHException(
                    "DafeApp system SSH key could not be loaded. "
                    "Check FIELD_ENCRYPTION_KEY in .env."
                )
            pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(private_key_str))
            transport.auth_publickey(username, pkey)

        else:
            from cloud.encryption import FieldEncryptor
            password = FieldEncryptor.decrypt(self.server.encrypted_password)
            transport.auth_password(username, password)

        # Wrap transport in an SSHClient so _run() (exec_command) works unchanged.
        client = paramiko.SSHClient()
        client._transport = transport
        return client

    def _run(self, client: paramiko.SSHClient, cmd: str) -> tuple[str, str]:
        """Execute *cmd* on the remote and return (stdout, stderr)."""
        _, stdout, stderr = client.exec_command(cmd, timeout=120)
        return stdout.read().decode(), stderr.read().decode()
