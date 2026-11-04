"""Fernet-based encrypt/decrypt helpers for PHI/PII stored at rest — the one
encryption policy for this app; no field should have a second, unencrypted
storage path.
"""
from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_fernet = Fernet(settings.encryptionKey.encode("utf-8")) if settings.encryptionKey else None


def encryptPii(plaintext: str) -> str:
    """Encrypt a PHI/PII value for storage.

    Returns:
        The Fernet ciphertext, as a UTF-8 string safe to store in a text column.

    Raises:
        RuntimeError: `ENCRYPTION_KEY` is not configured.
    """
    if not _fernet:
        raise RuntimeError("ENCRYPTION_KEY not configured")
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decryptPii(ciphertext: str) -> str:
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


# Every Fernet ciphertext starts with this (version byte 0x80 + timestamp, base64) — a public format marker, not a secret.
_FERNET_CIPHERTEXT_PREFIX = "gAAAAA"


def decryptStoredPii(value: str | None) -> str | None:
    """Readable value of a PHI column that may still hold legacy plaintext.

    Rows written before PII encryption existed still hold plaintext (e.g. 5
    of 21 MedTech-assigned `specimens.patient_name` values on 2026-09-27);
    those pass through unchanged. Anything that looks like a Fernet token is
    decrypted — and if it can't be (wrong key, corrupted), `None` is
    returned: ciphertext must never reach a client. Callers should log the
    record ID (never the value) when they get `None` for a non-empty input.

    Returns:
        The plaintext, or `None` for `None`/empty input or undecryptable
        ciphertext.

    Raises:
        RuntimeError: `ENCRYPTION_KEY` is not configured.
    """
    if not value:
        return None
    if not value.startswith(_FERNET_CIPHERTEXT_PREFIX):
        return value
    try:
        return decryptPii(value)
    except InvalidToken:
        return None
