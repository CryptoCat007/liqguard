"""Shared fixtures: a fully wired AppContext on a temporary database."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from liqguard.ai import MetaLLMClient
from liqguard.config import Settings
from liqguard.core import AppContext
from liqguard.storage import EncryptedStorage

BOT_TOKEN = "123456789:AAH-test-token-for-widget-hmac-checks"
ALICE, BOB = 1001, 2002


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        telegram_token=BOT_TOKEN,
        db_path=str(tmp_path / "liqguard.db"),
        key_file=tmp_path / "master.key",
        public_url="https://liqguard.test",
        bot_username="liqguard_test_bot",
    )


@pytest.fixture
async def ctx(settings):
    storage = EncryptedStorage(db_path=settings.db_path, encryption_key=EncryptedStorage.generate_key())
    context = await AppContext.create(
        settings, storage=storage, llm=MetaLLMClient(use_mock=True), simulation_options={"tick_interval": 0}
    )
    yield context
    await context.close()


def fake_message(user_id: int, text: str = "", chat_type: str = "private"):
    message = AsyncMock()
    message.from_user = MagicMock(id=user_id, username=f"user{user_id}")
    message.chat = MagicMock(type=chat_type, id=user_id)
    message.text = text
    return message


def fake_callback(user_id: int, data: str):
    callback = AsyncMock()
    callback.from_user = MagicMock(id=user_id)
    callback.data = data
    callback.message = AsyncMock()
    return callback
