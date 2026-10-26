"""Encrypted storage service using SQLite and Fernet encryption."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite
from cryptography.fernet import Fernet

from liqguard.models import ExchangeBinding, User, UserThreshold

logger = logging.getLogger(__name__)

DEFAULT_KEY_FILE = Path.home() / ".liqguard" / "encryption.key"


class EncryptionKeyError(Exception):
    """Raised when encryption key is missing or invalid for secret operations."""

    pass


class EncryptedStorage:
    """
    Encrypted storage for user data and API keys.

    Uses:
    - SQLite for persistence
    - Fernet symmetric encryption for secrets

    Security model:
    - API secrets are encrypted before storage
    - Encryption key should be from environment variable
    - Database file should have restricted permissions
    """

    def __init__(
        self,
        db_path: str = "liqguard.db",
        encryption_key: Optional[str] = None,
        key_file_path: Optional[Path] = None,
    ):
        """
        Initialize encrypted storage.

        Args:
            db_path: Path to SQLite database
            encryption_key: Base64-encoded Fernet key. If not provided,
                           reads from LIQGUARD_ENCRYPTION_KEY env var.
                           If still not found, loads or creates a key file.
            key_file_path: Path to key file (default: ~/.liqguard/encryption.key)
        """
        self.db_path = Path(db_path)
        self._key_file_path = key_file_path or DEFAULT_KEY_FILE
        self._key_source: str = "unknown"

        key = encryption_key or os.environ.get("LIQGUARD_ENCRYPTION_KEY")

        if key:
            self.fernet = Fernet(key.encode() if isinstance(key, str) else key)
            self._key_source = "env" if not encryption_key else "provided"
        else:
            key, self._key_source = self._load_or_create_key_file()
            self.fernet = Fernet(key.encode() if isinstance(key, str) else key)

        self._initialized = False

    def _load_or_create_key_file(self) -> tuple[str, str]:
        """
        Load encryption key from file, or create a new one.

        Returns:
            Tuple of (key, source) where source is 'file' or 'file_created'
        """
        if self._key_file_path.exists():
            try:
                key = self._key_file_path.read_text().strip()
                Fernet(key.encode())
                logger.info(f"Loaded encryption key from {self._key_file_path}")
                return key, "file"
            except Exception as e:
                logger.error(f"Invalid key file {self._key_file_path}: {e}")
                raise EncryptionKeyError(
                    f"Invalid encryption key in {self._key_file_path}. "
                    "Delete the file and restart, or set LIQGUARD_ENCRYPTION_KEY."
                ) from e

        self._key_file_path.parent.mkdir(parents=True, exist_ok=True)

        key = Fernet.generate_key().decode()
        self._key_file_path.write_text(key)

        try:
            self._key_file_path.chmod(0o600)
        except OSError:
            pass

        logger.warning(
            f"No LIQGUARD_ENCRYPTION_KEY set. "
            f"Generated and saved encryption key to {self._key_file_path}\n"
            f"⚠️  Keep this file safe! API secrets are encrypted with this key.\n"
            f"⚠️  If lost, you will need to re-bind your exchange accounts.\n"
            f"For production, set LIQGUARD_ENCRYPTION_KEY environment variable instead."
        )

        return key, "file_created"

    @property
    def key_source(self) -> str:
        """
        Return the source of the encryption key.

        Returns:
            One of: 'env', 'provided', 'file', 'file_created'
        """
        return self._key_source

    @property
    def is_key_persistent(self) -> bool:
        """Check if the encryption key is persistent (not ephemeral)."""
        return self._key_source in ("env", "provided", "file", "file_created")

    @staticmethod
    def generate_key() -> str:
        """Generate a new Fernet encryption key."""
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string."""
        return self.fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a string."""
        return self.fernet.decrypt(ciphertext.encode()).decode()

    async def initialize(self) -> None:
        """Initialize the database schema."""
        if self._initialized:
            return

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    language TEXT DEFAULT 'en',
                    is_demo_mode INTEGER DEFAULT 0,
                    threshold_json TEXT,
                    created_at TEXT,
                    last_active TEXT
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    venue TEXT NOT NULL,
                    binding_type TEXT NOT NULL,
                    api_key TEXT,
                    api_secret_encrypted TEXT,
                    wallet_address TEXT,
                    permissions_json TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT,
                    last_used TEXT,
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id),
                    UNIQUE(telegram_id, venue)
                )
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_bindings_user
                ON bindings(telegram_id)
            """)

            await db.commit()

        self._initialized = True
        logger.info(f"Database initialized at {self.db_path}")

    async def get_user(self, telegram_id: int) -> Optional[User]:
        """Get user by Telegram ID."""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            async with db.execute(
                "SELECT * FROM users WHERE telegram_id = ?",
                (telegram_id,)
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                return None

            threshold = UserThreshold()
            if row["threshold_json"]:
                try:
                    threshold = UserThreshold.from_dict(json.loads(row["threshold_json"]))
                except Exception as e:
                    logger.error(f"Error parsing threshold: {e}")

            bindings = await self._get_bindings(db, telegram_id)

            return User(
                telegram_id=telegram_id,
                username=row["username"],
                language=row["language"],
                is_demo_mode=bool(row["is_demo_mode"]),
                threshold=threshold,
                bindings=bindings,
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.utcnow(),
                last_active=datetime.fromisoformat(row["last_active"]) if row["last_active"] else datetime.utcnow(),
            )

    async def _get_bindings(
        self,
        db: aiosqlite.Connection,
        telegram_id: int
    ) -> list[ExchangeBinding]:
        """Get bindings for a user."""
        bindings = []

        async with db.execute(
            "SELECT * FROM bindings WHERE telegram_id = ?",
            (telegram_id,)
        ) as cursor:
            rows = await cursor.fetchall()

        for row in rows:
            api_secret = None
            if row[5]:
                try:
                    api_secret = self.decrypt(row[5])
                except Exception as e:
                    logger.error(f"Error decrypting secret: {e}")

            permissions = []
            if row[6]:
                try:
                    permissions = json.loads(row[6])
                except Exception:
                    pass

            binding = ExchangeBinding(
                venue=row[2],
                binding_type=row[3],
                api_key=row[4],
                api_secret_encrypted=row[5],
                wallet_address=row[7] if len(row) > 7 else None,
                permissions=permissions,
                is_active=bool(row[8] if len(row) > 8 else True),
                created_at=datetime.fromisoformat(row[9]) if len(row) > 9 and row[9] else datetime.utcnow(),
                last_used=datetime.fromisoformat(row[10]) if len(row) > 10 and row[10] else None,
            )
            binding.api_secret_encrypted = api_secret
            bindings.append(binding)

        return bindings

    async def save_user(self, user: User) -> None:
        """Save or update a user."""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO users (
                    telegram_id, username, language, is_demo_mode,
                    threshold_json, created_at, last_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    language = excluded.language,
                    is_demo_mode = excluded.is_demo_mode,
                    threshold_json = excluded.threshold_json,
                    last_active = excluded.last_active
            """, (
                user.telegram_id,
                user.username,
                user.language,
                int(user.is_demo_mode),
                json.dumps(user.threshold.to_dict()),
                user.created_at.isoformat(),
                user.last_active.isoformat(),
            ))

            await db.commit()

    async def save_binding(
        self,
        telegram_id: int,
        binding: ExchangeBinding
    ) -> None:
        """Save or update a binding."""
        await self.initialize()

        encrypted_secret = None
        if binding.api_secret_encrypted:
            encrypted_secret = self.encrypt(binding.api_secret_encrypted)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO bindings (
                    telegram_id, venue, binding_type, api_key,
                    api_secret_encrypted, wallet_address, permissions_json,
                    is_active, created_at, last_used
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id, venue) DO UPDATE SET
                    binding_type = excluded.binding_type,
                    api_key = excluded.api_key,
                    api_secret_encrypted = excluded.api_secret_encrypted,
                    wallet_address = excluded.wallet_address,
                    permissions_json = excluded.permissions_json,
                    is_active = excluded.is_active,
                    last_used = excluded.last_used
            """, (
                telegram_id,
                binding.venue,
                binding.binding_type,
                binding.api_key,
                encrypted_secret,
                binding.wallet_address,
                json.dumps(binding.permissions),
                int(binding.is_active),
                binding.created_at.isoformat(),
                binding.last_used.isoformat() if binding.last_used else None,
            ))

            await db.commit()

    async def delete_binding(self, telegram_id: int, venue: str) -> bool:
        """Delete a binding."""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM bindings WHERE telegram_id = ? AND venue = ?",
                (telegram_id, venue)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def update_threshold(
        self,
        telegram_id: int,
        threshold: UserThreshold
    ) -> None:
        """Update user's alert thresholds."""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE users SET threshold_json = ? WHERE telegram_id = ?
            """, (json.dumps(threshold.to_dict()), telegram_id))
            await db.commit()

    async def set_demo_mode(self, telegram_id: int, enabled: bool) -> None:
        """Set demo mode for a user."""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE users SET is_demo_mode = ? WHERE telegram_id = ?
            """, (int(enabled), telegram_id))
            await db.commit()

    async def get_all_users_with_bindings(self) -> list[User]:
        """Get all users who have active bindings."""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            async with db.execute("""
                SELECT DISTINCT u.telegram_id FROM users u
                INNER JOIN bindings b ON u.telegram_id = b.telegram_id
                WHERE b.is_active = 1
            """) as cursor:
                rows = await cursor.fetchall()

        users = []
        for row in rows:
            user = await self.get_user(row["telegram_id"])
            if user:
                users.append(user)

        return users

    async def close(self) -> None:
        """Close any open connections."""
        pass
