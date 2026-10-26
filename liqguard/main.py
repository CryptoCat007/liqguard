"""Main entry point for LiqGuard Telegram bot."""

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

from liqguard.llm import MetaLLMClient
from liqguard.services import (
    AlertManager,
    EncryptedStorage,
    MarketSimulation,
    ProtectiveActionsManager,
    RiskEngine,
)
from liqguard.telegram import BotHandlers, PositionMonitor, TelegramSimulationBridge, parse_chat_ids

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
        "dashboard_enabled": os.environ.get("LIQGUARD_DASHBOARD", "false").lower() == "true",
        "dashboard_port": int(os.environ.get("LIQGUARD_DASHBOARD_PORT", "8765")),
        "dashboard_host": os.environ.get("LIQGUARD_DASHBOARD_HOST", "127.0.0.1"),
        "demo_chat_ids": os.environ.get("LIQGUARD_DEMO_CHAT_IDS", ""),
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


async def main(dashboard: bool = False, dashboard_port: int = 8765) -> None:
    """Main entry point."""
    logger.info("Starting LiqGuard...")

    config = load_config()
    
    enable_dashboard = dashboard or config["dashboard_enabled"]
    port = dashboard_port if dashboard else config["dashboard_port"]

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

    actions_manager = ProtectiveActionsManager()
    simulation = MarketSimulation(llm_client=llm_client, actions_manager=actions_manager)
    sim_bridge = TelegramSimulationBridge(bot, simulation, parse_chat_ids(config["demo_chat_ids"]))

    handlers = BotHandlers(
        storage=storage,
        risk_engine=risk_engine,
        alert_manager=alert_manager,
        llm_client=llm_client,
        actions_manager=actions_manager,
        simulation=simulation,
        sim_bridge=sim_bridge,
    )
    handlers.register(dp, bot)

    monitor = PositionMonitor(
        bot=bot,
        storage=storage,
        alert_manager=alert_manager,
        llm_client=llm_client,
        poll_interval=config["poll_interval"],
    )

    dashboard_server: Optional["DashboardServer"] = None
    
    if enable_dashboard:
        from liqguard.dashboard import DashboardServer
        dashboard_server = DashboardServer(host=config["dashboard_host"], port=port, simulation=simulation)
        await dashboard_server.start()
        logger.info(f"Dashboard: http://{config['dashboard_host']}:{port}")
        logger.info(
            f"Simulation alerts go to {len(sim_bridge.chat_ids)} preset Telegram chat(s) "
            "plus anyone who enables /demo"
        )

    logger.info("LiqGuard started successfully!")
    logger.info(f"Meta AI: {'mock mode (demo/tests)' if llm_client.is_mock else 'live API'}")
    logger.info(f"Database: {config['db_path']}")
    logger.info(f"Poll interval: {config['poll_interval']}s")

    try:
        await monitor.start()

        await dp.start_polling(bot)
    finally:
        await simulation.stop(source="shutdown")
        await monitor.stop()
        await storage.close()
        await bot.session.close()
        if dashboard_server:
            await dashboard_server.stop()


def main_sync() -> None:
    """Synchronous entry point for console script."""
    parser = argparse.ArgumentParser(description="LiqGuard Telegram Bot")
    parser.add_argument(
        "--dashboard", 
        action="store_true",
        help="Enable web dashboard"
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8765,
        help="Dashboard port (default: 8765)"
    )
    args = parser.parse_args()
    
    try:
        asyncio.run(main(dashboard=args.dashboard, dashboard_port=args.dashboard_port))
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    main_sync()
