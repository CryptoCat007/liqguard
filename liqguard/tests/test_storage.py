"""Tests for master-key handling, envelope encryption and legacy migration."""

import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from liqguard.models import TESTNET, ExchangeBinding, User
from liqguard.storage import EncryptedStorage, EncryptionKeyError

API_KEY = "A" * 32 + "binancekey"
API_SECRET = "S" * 32 + "binancesecret"


@pytest.fixture(autouse=True)
def no_key_env(monkeypatch):
    monkeypatch.delenv("LIQGUARD_MASTER_KEY", raising=False)
    monkeypatch.delenv("LIQGUARD_ENCRYPTION_KEY", raising=False)


def make(tmp_path, **kw) -> EncryptedStorage:
    kw.setdefault("key_file_path", tmp_path / "keys" / "master.key")
    return EncryptedStorage(db_path=str(tmp_path / "test.db"), **kw)


class TestMasterKeySource:
    def test_key_from_new_env_var(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LIQGUARD_MASTER_KEY", EncryptedStorage.generate_key())
        storage = make(tmp_path)
        assert storage.key_source == "env"
        assert not (tmp_path / "keys" / "master.key").exists()

    def test_key_from_legacy_env_var(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LIQGUARD_ENCRYPTION_KEY", EncryptedStorage.generate_key())
        assert make(tmp_path).key_source == "env"

    def test_explicit_key_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LIQGUARD_MASTER_KEY", EncryptedStorage.generate_key())
        assert make(tmp_path, encryption_key=EncryptedStorage.generate_key()).key_source == "provided"

    def test_key_created_and_persisted_to_private_file(self, tmp_path):
        key_file = tmp_path / "keys" / "master.key"
        storage = make(tmp_path)
        assert storage.key_source == "file_created"
        assert storage.is_key_persistent
        assert len(key_file.read_text().strip()) == 44
        assert key_file.stat().st_mode & 0o777 == 0o600

    def test_key_loaded_from_existing_file(self, tmp_path):
        key_file = tmp_path / "keys" / "master.key"
        key_file.parent.mkdir()
        key_file.write_text(EncryptedStorage.generate_key())
        assert make(tmp_path).key_source == "file"

    def test_invalid_key_file_raises(self, tmp_path):
        key_file = tmp_path / "bad.key"
        key_file.write_text("invalid-key-not-base64")
        with pytest.raises(EncryptionKeyError, match="Invalid encryption key"):
            make(tmp_path, key_file_path=key_file)

    def test_env_beats_file(self, monkeypatch, tmp_path):
        key_file = tmp_path / "keys" / "master.key"
        key_file.parent.mkdir()
        key_file.write_text(EncryptedStorage.generate_key())
        monkeypatch.setenv("LIQGUARD_MASTER_KEY", EncryptedStorage.generate_key())
        assert make(tmp_path).key_source == "env"

    def test_generate_key_is_valid_fernet(self):
        key = EncryptedStorage.generate_key()
        assert len(key) == 44
        assert Fernet(key.encode()).decrypt(Fernet(key.encode()).encrypt(b"x")) == b"x"

    def test_repr_has_no_key_material(self, tmp_path):
        key = EncryptedStorage.generate_key()
        storage = make(tmp_path, encryption_key=key)
        assert key not in repr(storage)
        assert key not in repr(storage.keys)


class TestEnvelopeEncryption:
    async def save_binding(self, storage, user_id=1, **kw):
        await storage.save_user(User(telegram_id=user_id))
        binding = ExchangeBinding(venue="binance", binding_type="api_key", api_key=API_KEY, api_secret=API_SECRET, **kw)
        await storage.save_binding(user_id, binding)

    async def test_roundtrip_and_nothing_plaintext_on_disk(self, tmp_path):
        storage = make(tmp_path)
        await self.save_binding(storage)
        await storage.save_binding(1, ExchangeBinding(venue="hyperliquid", binding_type="wallet",
                                                      wallet_address="0x" + "ab" * 20))

        user = await storage.get_user(1)
        binance = user.get_binding("binance")
        assert (binance.api_key, binance.api_secret) == (API_KEY, API_SECRET)
        assert user.get_binding("hyperliquid").wallet_address == "0x" + "ab" * 20

        raw = Path(storage.db_path).read_bytes()
        for value in (API_KEY, API_SECRET, "ab" * 20):
            assert value.encode() not in raw

    async def test_each_user_gets_own_wrapped_data_key(self, tmp_path):
        storage = make(tmp_path)
        await self.save_binding(storage, 1)
        await self.save_binding(storage, 2)
        with sqlite3.connect(storage.db_path) as db:
            wrapped = [r[0] for r in db.execute("SELECT wrapped_dek FROM user_keys ORDER BY telegram_id")]
        assert len(wrapped) == 2 and wrapped[0] != wrapped[1]

    async def test_data_survives_restart_with_same_master_key(self, tmp_path):
        await self.save_binding(make(tmp_path))
        again = make(tmp_path)
        assert again.key_source == "file"
        assert (await again.get_user(1)).get_binding("binance").api_secret == API_SECRET

    async def test_wrong_master_key_cannot_read(self, tmp_path):
        await self.save_binding(make(tmp_path, encryption_key=EncryptedStorage.generate_key()))
        other = make(tmp_path, encryption_key=EncryptedStorage.generate_key())
        with pytest.raises(EncryptionKeyError):
            await other.get_user(1)

    async def test_master_key_rotation(self, tmp_path):
        old, new = EncryptedStorage.generate_key(), EncryptedStorage.generate_key()
        await self.save_binding(make(tmp_path, encryption_key=old))

        rotating = make(tmp_path, encryption_key=f"{new},{old}")
        assert await rotating.rotate_master_key() == 1

        only_new = make(tmp_path, encryption_key=new)
        assert (await only_new.get_user(1)).get_binding("binance").api_key == API_KEY

    async def test_testnet_and_mainnet_bindings_coexist(self, tmp_path):
        storage = make(tmp_path)
        await self.save_binding(storage)
        await storage.save_binding(1, ExchangeBinding(venue="binance", binding_type="api_key", network=TESTNET,
                                                      api_key="T" * 40, api_secret="U" * 40))
        user = await storage.get_user(1)
        assert user.get_binding("binance").api_key == API_KEY
        assert user.get_binding("binance", TESTNET).api_key == "T" * 40
        assert await storage.delete_binding(1, "binance", TESTNET)
        assert [b.network for b in (await storage.get_user(1)).bindings] == ["mainnet"]


class TestLegacyMigration:
    async def test_plaintext_keys_from_old_schema_are_reencrypted(self, tmp_path):
        master = EncryptedStorage.generate_key()
        db_path = tmp_path / "test.db"
        with sqlite3.connect(db_path) as db:
            db.executescript("""
                CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, username TEXT, language TEXT DEFAULT 'en',
                    is_demo_mode INTEGER DEFAULT 0, threshold_json TEXT, created_at TEXT, last_active TEXT);
                CREATE TABLE bindings (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL,
                    venue TEXT NOT NULL, binding_type TEXT NOT NULL, api_key TEXT, api_secret_encrypted TEXT,
                    wallet_address TEXT, permissions_json TEXT, is_active INTEGER DEFAULT 1, created_at TEXT,
                    last_used TEXT, UNIQUE(telegram_id, venue));
            """)
            db.execute("INSERT INTO users (telegram_id, username) VALUES (5, 'old')")
            db.execute(
                "INSERT INTO bindings (telegram_id, venue, binding_type, api_key, api_secret_encrypted, permissions_json) "
                "VALUES (5, 'binance', 'api_key', ?, ?, '[\"READ\"]')",
                (API_KEY, Fernet(master.encode()).encrypt(API_SECRET.encode()).decode()),
            )

        storage = make(tmp_path, encryption_key=master)
        user = await storage.get_user(5)
        assert user.get_binding("binance").api_secret == API_SECRET
        assert user.session_epoch == 0
        assert API_KEY.encode() not in db_path.read_bytes()
