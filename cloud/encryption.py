"""
Field-level encryption using Fernet symmetric encryption.
Key is loaded from settings.FIELD_ENCRYPTION_KEY (set via FIELD_ENCRYPTION_KEY env var).
In development, if the key is missing an ephemeral key is generated with a warning.
"""

import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

logger = logging.getLogger(__name__)

_encryptor = None


def _get_fernet() -> Fernet:
    global _encryptor
    if _encryptor is not None:
        return _encryptor

    key = getattr(settings, "FIELD_ENCRYPTION_KEY", "")
    if not key:
        key = Fernet.generate_key().decode()
        logger.warning(
            "FIELD_ENCRYPTION_KEY is not set. Using a one-time ephemeral key "
            "— encrypted values will NOT be recoverable after process restart. "
            "Set FIELD_ENCRYPTION_KEY in your environment for production use."
        )
    _encryptor = Fernet(key.encode() if isinstance(key, str) else key)
    return _encryptor


class FieldEncryptor:
    """Encrypt/decrypt single string values for database storage."""

    @staticmethod
    def encrypt(value: str) -> str:
        """Return a base64-encoded Fernet token for *value*."""
        if not value:
            return value
        return _get_fernet().encrypt(value.encode()).decode()

    @staticmethod
    def decrypt(value: str) -> str:
        """Return the original plaintext for *value* (a Fernet token)."""
        if not value:
            return value
        try:
            return _get_fernet().decrypt(value.encode()).decode()
        except (InvalidToken, Exception) as exc:
            logger.error("Failed to decrypt field value: %s", exc)
            return ""
