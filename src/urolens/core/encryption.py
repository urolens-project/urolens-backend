"""Fernet-based encrypt/decrypt helpers for PHI/PII stored at rest — the one
encryption policy for this app; no field should have a second, unencrypted
storage path.
"""
from cryptography.fernet import Fernet

from .config import settings

_fernet = Fernet(settings.encryption_key.encode("utf-8")) if settings.encryption_key else None


def encrypt_pii(plaintext: str) -> str:
    """Encrypt a PHI/PII value for storage.

    Returns:
        The Fernet ciphertext, as a UTF-8 string safe to store in a text column.

    Raises:
        RuntimeError: `ENCRYPTION_KEY` is not configured.
    """
    if not _fernet:
        raise RuntimeError("ENCRYPTION_KEY not configured")
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_pii(ciphertext: str) -> str:
    """Decrypt a value previously produced by `encrypt_pii`.

    Returns:
        The original plaintext.

    Raises:
        RuntimeError: `ENCRYPTION_KEY` is not configured.
        cryptography.fernet.InvalidToken: `ciphertext` is not valid Fernet
            output for the configured key (corrupted, tampered with, or
            encrypted under a different key).
    """
    if not _fernet:
        raise RuntimeError("ENCRYPTION_KEY not configured")
    return _fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
