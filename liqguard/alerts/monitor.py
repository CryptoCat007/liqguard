"""Background position monitor. Framework-free: delivery goes through a `Notifier`."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional, Protocol

from liqguard.models import Position, User
from liqguard.risk import RiskAssessment, RiskEngine

from .manager import AlertManager

logger = logging.getLogger(__name__)

SEVERITY_EMOJI = {"SAFE": "✅", "WARNING": "⚠️", "CRITICAL": "🚨", "EMERGENCY": "🆘"}


class Notifier(Protocol):
    async def notify(self, user_id: int, text: str) -> None:
        """Deliver `text` to exactly one user."""


class Explainer(Protocol):
    async def explain_risk(self, position, distance_pct, margin_to_safety, severity, language) -> str: ...


UsersProvider = Callable[[], Awaitable[list[User]]]
PositionsProvider = Callable[[User], Awaitable[list[Position]]]


def format_alert_message(position: Position, assessment: RiskAssessment, explanation: str) -> str:
    emoji = SEVERITY_EMOJI.get(assessment.risk_level_str, "❓")
    message = (
        f"{emoji} **{assessment.risk_level_str} ALERT**\n\n"
        f"**{position.symbol}** {position.side.value.upper()}\n"
        f"Distance to liquidation: **{assessment.distance_to_liquidation_pct:.1f}%**\n\n"
        f"{explanation}\n\n"
    )
    if assessment.margin_to_safety > 0:
        message += f"💰 Add **${assessment.margin_to_safety:,.2f}** margin for safety"
    return message


class PositionMonitor:
    """
    Polls each user's positions and alerts that user only.

    Every alert is computed from the user's own positions and thresholds and
    delivered to `notifier.notify(user.telegram_id, ...)`; there is no
    broadcast path.
    """

    def __init__(
        self,
        users: UsersProvider,
        positions: PositionsProvider,
        notifier: Notifier,
        alert_manager: AlertManager,
        explainer: Explainer,
        poll_interval: int = 30,
    ):
        self._users = users
        self._positions = positions
        self.notifier = notifier
        self.alert_manager = alert_manager
        self.explainer = explainer
        self.poll_interval = poll_interval
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info("Position monitor started (interval: %ss)", self.poll_interval)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_all_users()
            except Exception:
                logger.exception("Monitor loop error")
            await asyncio.sleep(self.poll_interval)

    async def check_all_users(self) -> None:
        for user in await self._users():
            try:
                await self.check_user(user)
            except Exception as e:
                logger.error("Error checking user %s: %s", user.telegram_id, type(e).__name__)

    async def check_user(self, user: User) -> int:
        """Check one user's positions; returns the number of alerts delivered."""
        positions = await self._positions(user)
        engine = RiskEngine(user.threshold)
        sent = 0
        for position in positions:
            assessment = engine.assess_position(position)
            if assessment.severity is None:
                continue
            alert = engine.create_alert(assessment)
            if alert and self.alert_manager.process_alert(user.telegram_id, alert, user.threshold.cooldown_seconds):
                explanation = await self.explainer.explain_risk(
                    position,
                    assessment.distance_to_liquidation_pct,
                    assessment.margin_to_safety,
                    assessment.severity,
                    user.language,
                )
                await self.notifier.notify(user.telegram_id, format_alert_message(position, assessment, explanation))
                sent += 1
        return sent
