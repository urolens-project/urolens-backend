from cryptography.fernet import Fernet

from .config import settings

_fernet = Fernet(settings.encryption_key.encode("utf-8")) if settings.encryption_key else None


def encrypt_pii(plaintext: str) -> str:
    if not _fernet:
        raise RuntimeError("ENCRYPTION_KEY not configured")
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_pii(ciphertext: str) -> str:
    if not _fernet:
        raise RuntimeError("ENCRYPTION_KEY not configured")
    return _fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
