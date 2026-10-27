"""
Envelope encryption.

- The master key (KEK) comes from LIQGUARD_MASTER_KEY (or the legacy name
  LIQGUARD_ENCRYPTION_KEY), or from a key file outside the repo. Several
  comma-separated keys enable rotation: the first encrypts, all decrypt.
- Every user gets a random data key (DEK). Only the DEK wrapped by the KEK is
  stored (`user_keys` table). The user's API key, secret and wallet address
  are encrypted with their own DEK, so a ciphertext copied into another
  user's row cannot be decrypted.
- Plaintext keys are never logged; objects here have redacted reprs.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

DEFAULT_KEY_FILE = Path.home() / ".liqguard" / "encryption.key"
KEY_ENV_VARS = ("LIQGUARD_MASTER_KEY", "LIQGUARD_ENCRYPTION_KEY")


class EncryptionKeyError(Exception):
    """Missing, invalid or non-matching encryption key."""


def generate_key() -> str:
    return Fernet.generate_key().decode()


def _parse_keys(raw: str, origin: str) -> list[str]:
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise EncryptionKeyError(f"No key found in {origin}")
    for key in keys:
        try:
            Fernet(key.encode())
        except Exception as e:
            raise EncryptionKeyError(
                f"Invalid encryption key in {origin}. Generate one with "
                "`python -c \"from liqguard.storage import generate_key; print(generate_key())\"`."
            ) from e
    return keys


def resolve_master_keys(explicit: Optional[str] = None, key_file: Optional[Path] = None) -> tuple[list[str], str]:
    """
    Find the master key(s). Returns (keys, source) where source is one of
    'provided', 'env', 'file', 'file_created'.
    """
    if explicit:
        return _parse_keys(explicit, "the provided key"), "provided"
    for name in KEY_ENV_VARS:
        raw = os.environ.get(name)
        if raw:
            return _parse_keys(raw, name), "env"

    path = Path(key_file or DEFAULT_KEY_FILE)
    if path.exists():
        keys = _parse_keys(path.read_text(), str(path))
        try:
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                logger.warning("Master key file %s is readable by other users (mode %o); run chmod 600", path, mode)
        except OSError:
            pass
        return keys, "file"

    path.parent.mkdir(parents=True, exist_ok=True)
    key = generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return [key], "file_created"


class KeyManager:
    """Wraps and unwraps per-user data keys with the master key."""

    def __init__(self, master_keys: list[str]):
        if not master_keys:
            raise EncryptionKeyError("At least one master key is required")
        self._kek = MultiFernet([Fernet(k.encode()) for k in master_keys])
        self._primary_bytes = master_keys[0].encode()

    def __repr__(self) -> str:
        return "KeyManager(<redacted>)"

    def new_data_key(self) -> tuple[Fernet, str]:
        """Return (dek, wrapped_dek)."""
        raw = Fernet.generate_key()
        return Fernet(raw), self._kek.encrypt(raw).decode()

    def unwrap(self, wrapped: str) -> Fernet:
        try:
            return Fernet(self._kek.decrypt(wrapped.encode()))
        except InvalidToken as e:
            raise EncryptionKeyError("Data key cannot be unwrapped with the configured master key(s)") from e

    def rewrap(self, wrapped: str) -> str:
        """Re-encrypt a wrapped DEK under the primary master key (key rotation)."""
        return self._kek.rotate(wrapped.encode()).decode()

    def decrypt_legacy(self, token: str) -> str:
        """Decrypt a value encrypted directly with the master key (pre-envelope databases)."""
        try:
            return self._kek.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise EncryptionKeyError("Legacy value cannot be decrypted with the configured master key(s)") from e

    def derive_secret(self, purpose: str) -> bytes:
        """Derive an independent 32-byte secret (e.g. session signing) from the primary master key."""
        return HKDF(
            algorithm=hashes.SHA256(), length=32, salt=b"liqguard-v1", info=purpose.encode()
        ).derive(self._primary_bytes)


def encrypt_with(dek: Fernet, plaintext: Optional[str]) -> Optional[str]:
    return dek.encrypt(plaintext.encode()).decode() if plaintext else None


def decrypt_with(dek: Fernet, token: Optional[str]) -> Optional[str]:
    return dek.decrypt(token.encode()).decode() if token else None
