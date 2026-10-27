"""Forwards one user's simulation events to that user's Telegram chat, and nowhere else."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from liqguard.simulation import MarketSimulation

logger = logging.getLogger(__name__)

LEVEL_EMOJI = {"warning": "⚠️", "critical": "🚨", "emergency": "🆘"}


def format_advice_message(card: dict) -> str:
    """Telegram text for one simulated alert + its Muse advice card."""
    level = card["level"]
    muse = "Muse (mock)" if card.get("mock") else "Muse"
    lines = [
        f"{LEVEL_EMOJI.get(level, '❓')} **{level.upper()} ALERT** · demo",
        "",
        f"**{card['symbol']}** {card['side'].upper()} on {card['venue'].title()}",
        f"Distance to liq: **{card['distance_pct']:.1f}%**",
        f"Mark: ${card['mark_price']:,.2f} · Liq: ${card['liquidation_price']:,.2f}",
        "",
        f"🧠 _{muse}:_ {card['explanation']}",
    ]
    if card.get("margin_to_add_usd"):
        lines += [
            "",
            f"💰 Add **${card['margin_to_add_usd']:,.2f}** margin → {card['target_distance_pct']:.0f}% from liq",
            f"✂️ Or reduce size by **{card['reduce_size_pct']:.1f}%**",
        ]
    return "\n".join(lines)


class TelegramSimulationBridge:
    """
    Subscribes to a user-owned MarketSimulation and sends its alerts to the
    owner's chat. The recipient is fixed to `simulation.owner_id`; there is
    no list of chats to broadcast to, and the public demo simulation (owner
    None) cannot be bridged.
    """

    def __init__(self, bot: Bot, simulation: MarketSimulation):
        if simulation.owner_id is None:
            raise ValueError("Public simulations are never forwarded to Telegram")
        self.bot = bot
        self.simulation = simulation
        self.chat_id: int = simulation.owner_id
        self.unsubscribe: Callable[[], None] = simulation.subscribe("telegram", self.on_event)

    async def _send(self, text: str, keyboard: Optional[InlineKeyboardMarkup] = None) -> None:
        try:
            await self.bot.send_message(self.chat_id, text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            logger.error("Failed to send simulation event to %s: %s", self.chat_id, type(e).__name__)

    async def on_event(self, event: dict) -> None:
        kind, data = event["type"], event["data"]

        if kind == "advice":
            keyboard = None
            if data.get("margin_to_add_usd"):
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=f"💰 Add ${data['margin_to_add_usd']:,.0f} margin",
                        callback_data=f"action:propose:{data['symbol']}",
                    ),
                    InlineKeyboardButton(text="❌ Dismiss", callback_data="action:dismiss"),
                ]])
            await self._send(format_advice_message(data), keyboard)

        elif kind == "audit" and data["event"] == "simulation_started":
            t = self.simulation.threshold
            await self._send(
                "🎬 **Market simulation started** (demo data)\n\n"
                "Prices will walk toward liquidation. Alerts fire at "
                f"⚠️ {t.warning_distance_pct:.0f}% · 🚨 {t.critical_distance_pct:.0f}% · "
                f"🆘 {t.emergency_distance_pct:.0f}%."
            )

        elif kind == "status" and data["state"] in ("finished", "stopped"):
            headline = (self.simulation.summary or {}).get("headline")
            text = f"⏹ **Simulation {data['state']}.**"
            if headline:
                text += f"\n\n🧠 {headline}"
            await self._send(text + "\n\nUse /risk for the portfolio overview.")


def attach_bridges(bot: Bot) -> Callable[[MarketSimulation], Callable[[], None]]:
    """Registry hook: give every user simulation a bridge to its owner's chat."""
    def hook(simulation: MarketSimulation) -> Callable[[], None]:
        return TelegramSimulationBridge(bot, simulation).unsubscribe
    return hook


class TelegramNotifier:
    """`alerts.Notifier` implementation: one message to one user."""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def notify(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(user_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.error("Failed to deliver alert to %s: %s", user_id, type(e).__name__)
