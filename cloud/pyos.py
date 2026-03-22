"""
PyOS service — validates and prepares a user-supplied VPS via SSH (paramiko).
"""

import logging
import socket
import io
from pathlib import Path

import paramiko

logger = logging.getLogger(__name__)

# Commands run sequentially during server preparation (bare-metal, no Docker)
PREPARE_COMMANDS = [
    "apt-get update -qq",
    "apt-get install -y -qq python3 python3-pip curl wget gnupg2 ufw",
    "ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw allow 8069 && ufw --force enable",
    "mkdir -p /opt/dafeapp/deployments",
]


def looks_like_public_key_text(value: str) -> bool:
    value = (value or "").strip()
    return value.startswith("ssh-") or value.startswith("ecdsa-") or value.startswith("sk-ssh-") or value.startswith("-----BEGIN")


def resolve_private_key_string(server) -> tuple[str | None, str]:
    """Return (private_key_str, source_label) for an ExternalServer-like object."""
    key_path = (getattr(server, "ssh_key_path", "") or "").strip()
    if key_path:
        if looks_like_public_key_text(key_path):
            raise paramiko.SSHException("SSH key path looks like a public key string, not a file path.")
        path = Path(key_path).expanduser()
        if not path.exists():
            raise paramiko.SSHException(f"SSH key path not found: {path}")
        return path.read_text(), str(path)

    try:
        from cloud.models import PyOSSSHSettings

        settings_obj = PyOSSSHSettings.get_or_create_settings()
        default_path = (settings_obj.default_ssh_key_path or "").strip()
        if default_path:
            if looks_like_public_key_text(default_path):
                raise paramiko.SSHException(
                    "Default SSH key path looks like a public key string, not a file path."
                )
            path = Path(default_path).expanduser()
            if not path.exists():
                raise paramiko.SSHException(f"Default SSH key path not found: {path}")
            return path.read_text(), str(path)
    except ImportError:
        pass

    from cloud.models import SystemSSHKey

    system_key = SystemSSHKey.get_or_create_keypair()
    private_key_str = system_key.get_private_key()
    return private_key_str, "DafeApp system SSH key"


def load_private_key_from_string(private_key_str: str, source: str):
    """
    Parse a private key string using the common Paramiko key types.

    This keeps us from assuming every valid SSH key is Ed25519 and gives a
    clearer error when the file is actually public-key text or is encrypted.
    """
    key_types = (
        paramiko.Ed25519Key,
        paramiko.RSAKey,
        paramiko.ECDSAKey,
    )
    last_error: Exception | None = None
    for key_cls in key_types:
        try:
            return key_cls.from_private_key(io.StringIO(private_key_str))
        except Exception as exc:
            last_error = exc

    if looks_like_public_key_text(private_key_str):
        raise paramiko.SSHException(
            f"SSH key source {source} contains public key text, not a private key."
        )
    raise paramiko.SSHException(
        f"SSH key source {source} is not a supported private key format: {last_error}"
    )


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
        target = f"{self.server.username}@{self.server.host}:{self.server.port}"
        auth_label = self.server.auth_type or "UNKNOWN"
        client = None
        try:
            logger.info("SSH validation started for %s using %s", target, auth_label)
            client = self._get_client()
            stdout, stderr = self._run(client, "echo OK")
            if stdout.strip() == "OK":
                logger.info("SSH validation succeeded for %s", target)
                return True, "SSH connection successful."
            logger.warning("SSH validation unexpected output for %s: %r", target, stdout.strip())
            return False, f"Unexpected output: {stdout!r}"
        except paramiko.AuthenticationException as exc:
            logger.warning("SSH validation authentication failed for %s: %s", target, exc)
            return False, f"Authentication failed for {target} using {auth_label}: {exc}"
        except (socket.timeout, paramiko.ssh_exception.NoValidConnectionsError) as exc:
            logger.warning("SSH validation host unreachable for %s: %s", target, exc)
            return False, f"Host unreachable for {target}: {exc}"
        except Exception as exc:
            logger.exception("SSH validate error for server %s", self.server.pk)
            return False, f"SSH validation error for {target}: {type(exc).__name__}: {exc}"
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
        target = f"{self.server.username}@{self.server.host}:{self.server.port}"
        try:
            logger.info("SSH preparation started for %s (%d commands)", target, len(PREPARE_COMMANDS))
            client = self._get_client()
            for idx, cmd in enumerate(PREPARE_COMMANDS, start=1):
                logger.info("SSH preparation step %d/%d for %s: %s", idx, len(PREPARE_COMMANDS), target, cmd)
                log_lines.append(f"$ {cmd}")
                stdout, stderr = self._run(client, cmd)
                if stdout:
                    log_lines.append(stdout.rstrip())
                if stderr:
                    log_lines.append(f"[stderr] {stderr.rstrip()}")
            logger.info("SSH preparation finished for %s", target)
            return True, "\n".join(log_lines)
        except paramiko.AuthenticationException:
            msg = "Authentication failed during preparation."
            log_lines.append(msg)
            logger.warning("SSH preparation authentication failed for %s", target)
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
            private_key_str, source = resolve_private_key_string(self.server)
            if not private_key_str:
                transport.close()
                raise paramiko.ssh_exception.SSHException(
                    f"DafeApp SSH key could not be loaded from {source}. "
                    "Check the configured SSH key path or FIELD_ENCRYPTION_KEY in .env."
                )
            pkey = load_private_key_from_string(private_key_str, source)
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
