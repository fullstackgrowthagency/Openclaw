"""Encryption at rest for BrokerCredential's app_key/app_secret/account_id
-- these are real brokerage credentials, not stored in plaintext. Fernet
(symmetric, authenticated encryption) keyed by Settings.credential_encryption_key
is deliberately the whole mechanism here, not a KMS/Vault integration: this
runs on a single small VPS with one operator, where the actual requirement
is "not plaintext at rest, tamper-evident, rotatable," which Fernet gives
directly -- a full KMS would add real operational surface (a second
service, IAM policy, network dependency) for no benefit at this scale.

Generate a key once with `Fernet.generate_key()` and set it as
CREDENTIAL_ENCRYPTION_KEY in the environment -- never commit it, never
store it in the database next to the ciphertext it protects."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from ..config import Settings


class CredentialEncryptionError(RuntimeError):
    pass


def _fernet(settings: Settings) -> Fernet:
    if not settings.credential_encryption_key:
        raise CredentialEncryptionError(
            "CREDENTIAL_ENCRYPTION_KEY is not set -- refusing to encrypt or decrypt broker "
            "credentials with no key. Generate one with `python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\"` and set it in the environment."
        )
    try:
        return Fernet(settings.credential_encryption_key.encode())
    except (ValueError, TypeError) as exc:
        raise CredentialEncryptionError(f"CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc


def encrypt_secret(plaintext: str, settings: Settings) -> str:
    return _fernet(settings).encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str, settings: Settings) -> str:
    try:
        return _fernet(settings).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        # Wrong/rotated key, or the ciphertext was tampered with -- either
        # way this is not recoverable here, and the caller (e.g. the
        # broker-credential verify flow) needs a clear reason to surface,
        # not a raw cryptography-library exception.
        raise CredentialEncryptionError(
            "Could not decrypt this credential -- CREDENTIAL_ENCRYPTION_KEY may have changed "
            "since it was stored, or the stored value is corrupted."
        ) from exc
