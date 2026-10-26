"""Position monitor service for background alerts."""

import asyncio
import logging
from typing import Optional

from aiogram import Bot

from liqguard.connectors import BinanceConnector, DemoConnector, HyperliquidConnector
from liqguard.llm import MetaLLMClient
from liqguard.models import Position, User
from liqguard.services import AlertManager, EncryptedStorage, RiskEngine

logger = logging.getLogger(__name__)


class PositionMonitor:
    """
    Background service that monitors positions and sends alerts.

    Features:
    - Polls positions at configurable intervals
    - Checks against user thresholds
    - Sends Telegram alerts with risk explanations
    - Respects cooldown periods
    """

    def __init__(
        self,
        bot: Bot,
        storage: EncryptedStorage,
        alert_manager: AlertManager,
        llm_client: MetaLLMClient,
        poll_interval: int = 30,
    ):
        """
        Initialize position monitor.

        Args:
            bot: Telegram bot instance
            storage: Encrypted storage service
            alert_manager: Alert manager for cooldowns
            llm_client: LLM client for explanations
            poll_interval: Seconds between position checks
        """
        self.bot = bot
        self.storage = storage
        self.alert_manager = alert_manager
        self.llm = llm_client
        self.poll_interval = poll_interval

        self._running = False
        self._task: Optional[asyncio.Task] = None

        self._demo_connectors: dict[int, DemoConnector] = {}

    async def start(self) -> None:
        """Start the position monitor."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(f"Position monitor started (interval: {self.poll_interval}s)")

    async def stop(self) -> None:
        """Stop the position monitor."""
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("Position monitor stopped")

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                await self._check_all_users()
            except Exception as e:
                logger.error(f"Error in monitor loop: {e}")

            await asyncio.sleep(self.poll_interval)

    async def _check_all_users(self) -> None:
        """Check positions for all users with bindings."""
        users = await self.storage.get_all_users_with_bindings()

        for user in users:
            try:
                await self._check_user_positions(user)
            except Exception as e:
                logger.error(f"Error checking user {user.telegram_id}: {e}")

    async def _check_user_positions(self, user: User) -> None:
        """Check positions for a single user."""
        positions = []

        if user.is_demo_mode:
            if user.telegram_id not in self._demo_connectors:
                self._demo_connectors[user.telegram_id] = DemoConnector("volatile")
            positions = await self._demo_connectors[user.telegram_id].get_positions()
        else:
            for binding in user.bindings:
                if not binding.is_active:
                    continue

                try:
                    if binding.venue == "binance":
                        connector = BinanceConnector(
                            binding.api_key,
                            binding.api_secret_encrypted
                        )
                    elif binding.venue == "hyperliquid":
                        connector = HyperliquidConnector(binding.wallet_address)
                    else:
                        continue

                    venue_positions = await connector.get_positions()
                    positions.extend(venue_positions)
                    await connector.close()

                except Exception as e:
                    logger.error(f"Error fetching from {binding.venue}: {e}")

        if not positions:
            return

        risk_engine = RiskEngine(user.threshold)

        for position in positions:
            assessment = risk_engine.assess_position(position)

            if assessment.severity is None:
                continue

            alert = risk_engine.create_alert(assessment)
            if not alert:
                continue

            processed = self.alert_manager.process_alert(
                alert,
                cooldown_seconds=user.threshold.cooldown_seconds
            )

            if processed:
                await self._send_alert(user, position, assessment)

    async def _send_alert(
        self,
        user: User,
        position: Position,
        assessment
    ) -> None:
        """Send an alert to the user."""
        try:
            explanation = await self.llm.explain_risk(
                position,
                assessment.distance_to_liquidation_pct,
                assessment.margin_to_safety,
                assessment.severity,
                user.language
            )

            severity_emoji = {
                "SAFE": "✅",
                "WARNING": "⚠️",
                "CRITICAL": "🚨",
                "EMERGENCY": "🆘",
            }.get(assessment.risk_level_str, "❓")

            message = (
                f"{severity_emoji} **{assessment.risk_level_str} ALERT**\n\n"
                f"**{position.symbol}** {position.side.value.upper()}\n"
                f"Distance to liquidation: **{assessment.distance_to_liquidation_pct:.1f}%**\n\n"
                f"{explanation}\n\n"
            )

            if assessment.margin_to_safety > 0:
                message += f"💰 Add **${assessment.margin_to_safety:,.2f}** margin for safety"

            await self.bot.send_message(
                user.telegram_id,
                message,
                parse_mode="Markdown"
            )

            logger.info(f"Alert sent to {user.telegram_id}: {position.symbol}")

        except Exception as e:
            logger.error(f"Error sending alert to {user.telegram_id}: {e}")

    async def check_user_now(self, user_id: int) -> None:
        """Manually trigger a check for a specific user."""
        user = await self.storage.get_user(user_id)
        if user:
            await self._check_user_positions(user)
