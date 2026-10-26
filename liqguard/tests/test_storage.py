"""Tests for encrypted storage encryption key handling."""

import tempfile
from pathlib import Path

import pytest

from liqguard.services.storage import EncryptedStorage, EncryptionKeyError


class TestEncryptionKeyHandling:
    """Test encryption key loading, generation, and persistence."""

    def test_key_from_env_var(self, monkeypatch):
        """Test that key is loaded from LIQGUARD_ENCRYPTION_KEY env var."""
        test_key = EncryptedStorage.generate_key()
        monkeypatch.setenv("LIQGUARD_ENCRYPTION_KEY", test_key)

        with tempfile.TemporaryDirectory() as tmpdir:
            storage = EncryptedStorage(
                db_path=f"{tmpdir}/test.db",
                key_file_path=Path(tmpdir) / "key.txt"
            )

            assert storage.key_source == "env"
            assert storage.is_key_persistent

    def test_key_from_explicit_argument(self, monkeypatch):
        """Test that explicitly provided key takes precedence."""
        monkeypatch.delenv("LIQGUARD_ENCRYPTION_KEY", raising=False)
        test_key = EncryptedStorage.generate_key()

        with tempfile.TemporaryDirectory() as tmpdir:
            storage = EncryptedStorage(
                db_path=f"{tmpdir}/test.db",
                encryption_key=test_key,
                key_file_path=Path(tmpdir) / "key.txt"
            )

            assert storage.key_source == "provided"
            assert storage.is_key_persistent

    def test_key_created_and_persisted_to_file(self, monkeypatch):
        """Test that key is generated and saved to file when not provided."""
        monkeypatch.delenv("LIQGUARD_ENCRYPTION_KEY", raising=False)

        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / "subdir" / "encryption.key"

            storage = EncryptedStorage(
                db_path=f"{tmpdir}/test.db",
                key_file_path=key_file
            )

            assert storage.key_source == "file_created"
            assert storage.is_key_persistent
            assert key_file.exists()

            key_content = key_file.read_text().strip()
            assert len(key_content) == 44

    def test_key_loaded_from_existing_file(self, monkeypatch):
        """Test that key is loaded from existing key file."""
        monkeypatch.delenv("LIQGUARD_ENCRYPTION_KEY", raising=False)
        test_key = EncryptedStorage.generate_key()

        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / "encryption.key"
            key_file.write_text(test_key)

            storage = EncryptedStorage(
                db_path=f"{tmpdir}/test.db",
                key_file_path=key_file
            )

            assert storage.key_source == "file"
            assert storage.is_key_persistent

    def test_invalid_key_file_raises_error(self, monkeypatch):
        """Test that invalid key file raises EncryptionKeyError."""
        monkeypatch.delenv("LIQGUARD_ENCRYPTION_KEY", raising=False)

        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / "encryption.key"
            key_file.write_text("invalid-key-not-base64")

            with pytest.raises(EncryptionKeyError) as exc_info:
                EncryptedStorage(
                    db_path=f"{tmpdir}/test.db",
                    key_file_path=key_file
                )

            assert "Invalid encryption key" in str(exc_info.value)

    def test_key_persists_across_instances(self, monkeypatch):
        """Test that generated key persists and is reused."""
        monkeypatch.delenv("LIQGUARD_ENCRYPTION_KEY", raising=False)

        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / "encryption.key"

            storage1 = EncryptedStorage(
                db_path=f"{tmpdir}/test.db",
                key_file_path=key_file
            )
            assert storage1.key_source == "file_created"

            encrypted = storage1.encrypt("secret data")

            storage2 = EncryptedStorage(
                db_path=f"{tmpdir}/test.db",
                key_file_path=key_file
            )
            assert storage2.key_source == "file"

            decrypted = storage2.decrypt(encrypted)
            assert decrypted == "secret data"

    def test_env_var_takes_precedence_over_file(self, monkeypatch):
        """Test that env var key takes precedence over key file."""
        env_key = EncryptedStorage.generate_key()
        file_key = EncryptedStorage.generate_key()

        monkeypatch.setenv("LIQGUARD_ENCRYPTION_KEY", env_key)

        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / "encryption.key"
            key_file.write_text(file_key)

            storage = EncryptedStorage(
                db_path=f"{tmpdir}/test.db",
                key_file_path=key_file
            )

            assert storage.key_source == "env"

    def test_generate_key_produces_valid_fernet_key(self):
        """Test that generate_key produces valid Fernet keys."""
        from cryptography.fernet import Fernet

        key = EncryptedStorage.generate_key()

        assert isinstance(key, str)
        assert len(key) == 44

        fernet = Fernet(key.encode())
        test_data = b"test encryption"
        encrypted = fernet.encrypt(test_data)
        decrypted = fernet.decrypt(encrypted)
        assert decrypted == test_data


class TestEncryptDecrypt:
    """Test encrypt/decrypt functionality."""

    def test_encrypt_decrypt_roundtrip(self):
        """Test that encrypt then decrypt returns original data."""
        key = EncryptedStorage.generate_key()

        with tempfile.TemporaryDirectory() as tmpdir:
            storage = EncryptedStorage(
                db_path=f"{tmpdir}/test.db",
                encryption_key=key
            )

            original = "my secret api key 12345"
            encrypted = storage.encrypt(original)
            decrypted = storage.decrypt(encrypted)

            assert decrypted == original
            assert encrypted != original

    def test_different_keys_cannot_decrypt(self):
        """Test that data encrypted with one key cannot be decrypted with another."""
        from cryptography.fernet import InvalidToken

        key1 = EncryptedStorage.generate_key()
        key2 = EncryptedStorage.generate_key()

        with tempfile.TemporaryDirectory() as tmpdir:
            storage1 = EncryptedStorage(
                db_path=f"{tmpdir}/test1.db",
                encryption_key=key1
            )
            storage2 = EncryptedStorage(
                db_path=f"{tmpdir}/test2.db",
                encryption_key=key2
            )

            encrypted = storage1.encrypt("secret")

            with pytest.raises(InvalidToken):
                storage2.decrypt(encrypted)
