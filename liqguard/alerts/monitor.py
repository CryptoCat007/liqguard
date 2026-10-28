"""Background position monitor. Framework-free: delivery goes through a `Notifier`."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional, Protocol, Union

from liqguard.models import Position, User
from liqguard.risk import (
    PreFundingWarning,
    RiskAssessment,
    RiskContext,
    RiskEngine,
    pre_funding_warning,
)

from .manager import AlertManager

logger = logging.getLogger(__name__)

SEVERITY_EMOJI = {"SAFE": "✅", "WARNING": "⚠️", "CRITICAL": "🚨", "EMERGENCY": "🆘"}


Action = tuple[str, str]
"""(button label, callback data) offered with an alert."""


class Notifier(Protocol):
    async def notify(self, user_id: int, text: str, actions: Optional[list[Action]] = None) -> None:
        """Deliver `text` (and optional action buttons) to exactly one user."""


class BookSnapshotLike(Protocol):
    positions: list[Position]
    context: RiskContext


class Explainer(Protocol):
    async def explain_risk(self, position, distance_pct, margin_to_safety, severity, language) -> str: ...


UsersProvider = Callable[[], Awaitable[list[User]]]
PositionsProvider = Callable[[User], Awaitable[Union[list[Position], BookSnapshotLike]]]


def rescue_action(position: Position) -> Action:
    return ("🛟 Rescue options", f"rescue:{position.venue.value}:{position.symbol}")


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


def format_prefunding_message(warning: dict, demo: bool = False) -> str:
    """Pre-funding warning text from `PreFundingWarning.to_dict()` (every number computed in `risk.funding`)."""
    liq_before, liq_after = warning.get("liquidation_before"), warning.get("liquidation_after")
    lines = [
        f"⏳ **PRE-FUNDING ALERT**{' · demo' if demo else ''}",
        "",
        f"**{warning['symbol']}** {warning['side'].upper()} on {warning['venue'].title()} pays "
        f"**${warning['payment_usd']:,.2f}** funding in **{warning['minutes_to_settlement']:.0f} min** "
        f"({warning['rate_pct']:+.4f}% per {warning['interval_hours']:g}h, current rate).",
        f"Distance to liq: **{warning['distance_before_pct']:.1f}% → {warning['distance_after_pct']:.1f}%** after settlement",
    ]
    if liq_before and liq_after:
        lines.append(f"Liq: ${liq_before:,.2f} → ${liq_after:,.2f}")
    lines.append(
        f"That is {warning['buffer_share_pct']:.1f}% of your remaining margin buffer"
        + (" and moves you into a worse alert tier." if warning.get("crosses_tier") else ".")
    )
    return "\n".join(lines)


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
        prefunding_lead_minutes: float = 30.0,
    ):
        self._users = users
        self._positions = positions
        self.notifier = notifier
        self.alert_manager = alert_manager
        self.explainer = explainer
        self.poll_interval = poll_interval
        self.prefunding_lead_minutes = prefunding_lead_minutes
        self._task: Optional[asyncio.Task] = None
        self._prefunding_sent: dict[tuple[int, str, str, datetime], datetime] = {}

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
        result = await self._positions(user)
        if isinstance(result, list):
            positions, context = result, None
        else:
            positions, context = result.positions, result.context
        engine = RiskEngine(user.threshold, context)
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
                await self.notifier.notify(
                    user.telegram_id,
                    format_alert_message(position, assessment, explanation),
                    actions=[rescue_action(position)],
                )
                sent += 1
        if context is not None:
            sent += await self._check_prefunding(user, positions, context)
        return sent

    async def _check_prefunding(self, user: User, positions: list[Position], context: RiskContext) -> int:
        """One pre-funding warning per position per settlement."""
        sent = 0
        for key, settlement in list(self._prefunding_sent.items()):
            if settlement < context.now:
                del self._prefunding_sent[key]
        for position in positions:
            if not context.computable(position):
                continue
            warning: Optional[PreFundingWarning] = pre_funding_warning(
                position, context.account_for(position), context.funding_for, context.now,
                user.threshold, self.prefunding_lead_minutes,
            )
            if warning is None:
                continue
            key = (user.telegram_id, position.venue.value, position.symbol, warning.settlement_time)
            if key in self._prefunding_sent:
                continue
            self._prefunding_sent[key] = warning.settlement_time
            await self.notifier.notify(
                user.telegram_id, format_prefunding_message(warning.to_dict()), actions=[rescue_action(position)]
            )
            sent += 1
        return sent
