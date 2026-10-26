"""Main entry point for LiqGuard Telegram bot."""

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

from liqguard.llm import MetaLLMClient
from liqguard.services import AlertManager, EncryptedStorage, RiskEngine
from liqguard.telegram import BotHandlers, PositionMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)

logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("aiogram").setLevel(logging.INFO)


def load_config() -> dict:
    """Load configuration from environment variables."""
    load_dotenv()

    config = {
        "telegram_token": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "meta_api_key": os.environ.get("META_API_KEY"),
        "encryption_key": os.environ.get("LIQGUARD_ENCRYPTION_KEY"),
        "db_path": os.environ.get("LIQGUARD_DB_PATH", "liqguard.db"),
        "poll_interval": int(os.environ.get("LIQGUARD_POLL_INTERVAL", "30")),
    }

    if not config["telegram_token"]:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is required")
        sys.exit(1)

    if not config["meta_api_key"]:
        logger.warning(
            "META_API_KEY not set. AI features (risk explanations, screenshot parsing) "
            "will use fallback mode."
        )

    if not config["encryption_key"]:
        logger.warning(
            "LIQGUARD_ENCRYPTION_KEY not set. Using ephemeral key. "
            "API secrets will not persist across restarts."
        )

    return config


async def main() -> None:
    """Main entry point."""
    logger.info("Starting LiqGuard...")

    config = load_config()

    storage = EncryptedStorage(
        db_path=config["db_path"],
        encryption_key=config["encryption_key"]
    )
    await storage.initialize()

    risk_engine = RiskEngine()
    alert_manager = AlertManager()
    llm_client = MetaLLMClient(api_key=config["meta_api_key"])

    bot = Bot(
        token=config["telegram_token"],
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
    )

    dp = Dispatcher(storage=MemoryStorage())

    handlers = BotHandlers(
        storage=storage,
        risk_engine=risk_engine,
        alert_manager=alert_manager,
        llm_client=llm_client,
    )
    handlers.register(dp, bot)

    monitor = PositionMonitor(
        bot=bot,
        storage=storage,
        alert_manager=alert_manager,
        llm_client=llm_client,
        poll_interval=config["poll_interval"],
    )

    logger.info("LiqGuard started successfully!")
    logger.info(f"Meta AI: {'mock mode (demo/tests)' if llm_client.is_mock else 'live API'}")
    logger.info(f"Database: {config['db_path']}")
    logger.info(f"Poll interval: {config['poll_interval']}s")

    try:
        await monitor.start()

        await dp.start_polling(bot)
    finally:
        await monitor.stop()
        await storage.close()
        await bot.session.close()


def main_sync() -> None:
    """Synchronous entry point for console script."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    main_sync()
