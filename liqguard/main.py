"""Entry point: Telegram bot + position monitor (+ optional web dashboard)."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

from liqguard.alerts import PositionMonitor
from liqguard.config import Settings
from liqguard.core import AppContext
from liqguard.logsafe import install_log_redaction
from liqguard.storage import EncryptedStorage, EncryptionKeyError
from liqguard.telegram import BotHandlers, TelegramNotifier, attach_bridges

logger = logging.getLogger("liqguard")


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)


def describe_key_source(storage: EncryptedStorage) -> tuple[int, str]:
    """(log level, message) describing where the master key came from. Never includes the key."""
    source = storage.key_source
    if source == "env":
        return logging.INFO, "Master key loaded from environment (LIQGUARD_MASTER_KEY / LIQGUARD_ENCRYPTION_KEY)."
    if source == "provided":
        return logging.INFO, "Master key provided programmatically."
    if source == "file":
        return logging.INFO, f"Master key loaded from {storage.key_file_path}."
    return logging.WARNING, (
        f"No master key configured: generated a new one and saved it to {storage.key_file_path} (mode 600). "
        "It persists across restarts. Back it up — losing it makes stored exchange keys unreadable — "
        "or set LIQGUARD_MASTER_KEY in production."
    )


def load_settings() -> Settings:
    load_dotenv()
    settings = Settings.from_env()
    if not settings.telegram_token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is required")
        sys.exit(1)
    return settings


async def main(dashboard: bool = False, dashboard_port: Optional[int] = None) -> None:
    configure_logging()
    settings = load_settings()
    if dashboard:
        settings = dataclasses.replace(settings, dashboard_enabled=True)
    if dashboard_port:
        settings = dataclasses.replace(settings, dashboard_port=dashboard_port)
    install_log_redaction(settings.telegram_token, settings.meta_api_key, settings.session_secret)

    logger.info("Starting LiqGuard...")
    try:
        ctx = await AppContext.create(settings)
    except EncryptionKeyError as e:
        logger.error("%s", e)
        sys.exit(1)
    level, message = describe_key_source(ctx.storage)
    logger.log(level, message)

    bot = Bot(token=settings.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    if not settings.bot_username:
        try:
            me = await bot.get_me()
            ctx.settings = dataclasses.replace(ctx.settings, bot_username=me.username)
        except Exception as e:
            logger.warning("Could not read bot username (Telegram Login Widget disabled): %s", type(e).__name__)

    ctx.simulations.on_create(attach_bridges(bot))

    dp = Dispatcher(storage=MemoryStorage())
    BotHandlers(ctx).register(dp, bot)

    monitor = PositionMonitor(
        users=ctx.storage.get_all_users_with_bindings,
        positions=ctx.positions.monitored_positions,
        notifier=TelegramNotifier(bot),
        alert_manager=ctx.alerts,
        explainer=ctx.llm,
        poll_interval=settings.poll_interval,
    )

    dashboard_server = None
    if settings.dashboard_enabled:
        from liqguard.web import DashboardServer

        dashboard_server = DashboardServer(ctx, host=settings.dashboard_host, port=settings.dashboard_port)
        await dashboard_server.start()
        logger.info(
            "Dashboard: http://%s:%s (public demo at /, /dashboard in Telegram for a private login link to %s)",
            settings.dashboard_host, settings.dashboard_port, settings.dashboard_url,
        )

    logger.info("Muse: %s", "mock mode" if ctx.llm.is_mock else "live API")
    logger.info("Database: %s · poll interval %ss · protective actions %s",
                settings.db_path, settings.poll_interval, "enabled (testnet)" if ctx.actions.is_enabled() else "disabled")

    try:
        await monitor.start()
        await dp.start_polling(bot)
    finally:
        await monitor.stop()
        await ctx.close()
        await bot.session.close()
        if dashboard_server:
            await dashboard_server.stop()


def main_sync() -> None:
    parser = argparse.ArgumentParser(description="LiqGuard Telegram bot")
    parser.add_argument("--dashboard", action="store_true", help="Also serve the web dashboard")
    parser.add_argument("--dashboard-port", type=int, default=None, help="Dashboard port (default: 8765)")
    args = parser.parse_args()
    try:
        asyncio.run(main(dashboard=args.dashboard, dashboard_port=args.dashboard_port))
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    main_sync()
