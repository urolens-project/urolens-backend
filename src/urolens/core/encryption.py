from cryptography.fernet import Fernet

from app.config import ENCRYPTION_KEY

_fernet = Fernet(ENCRYPTION_KEY.encode("utf-8")) if ENCRYPTION_KEY else None


def encrypt_pii(plaintext: str) -> str:
    if not _fernet:
        raise RuntimeError("ENCRYPTION_KEY not configured")
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_pii(ciphertext: str) -> str:
    if not _fernet:
        raise RuntimeError("ENCRYPTION_KEY not configured")
    return _fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
