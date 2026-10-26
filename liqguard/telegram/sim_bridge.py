"""Forwards shared demo-simulation events to Telegram chats."""

import logging
from typing import Iterable, Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from liqguard.services.simulation import MarketSimulation

logger = logging.getLogger(__name__)

LEVEL_EMOJI = {"warning": "⚠️", "critical": "🚨", "emergency": "🆘"}


def parse_chat_ids(raw: Optional[str]) -> set[int]:
    """Parse a comma-separated LIQGUARD_DEMO_CHAT_IDS value."""
    ids = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


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
    """Subscribes to a MarketSimulation and mirrors its alerts to Telegram chats."""

    def __init__(self, bot: Bot, simulation: MarketSimulation, chat_ids: Iterable[int] = ()):
        self.bot = bot
        self.simulation = simulation
        self.chat_ids: set[int] = set(chat_ids)
        simulation.subscribe("telegram", self.on_event)

    def add_chat(self, chat_id: int) -> None:
        self.chat_ids.add(chat_id)

    def has_chat(self, chat_id: int) -> bool:
        return chat_id in self.chat_ids

    async def _send(self, text: str, keyboard: Optional[InlineKeyboardMarkup] = None) -> None:
        for chat_id in list(self.chat_ids):
            try:
                await self.bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Failed to send simulation event to {chat_id}: {e}")

    async def on_event(self, event: dict) -> None:
        if not self.chat_ids:
            return
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
            await self._send(
                "🎬 **Market simulation started** (demo data)\n\n"
                "Prices will walk toward liquidation. Alerts fire at "
                f"⚠️ {self.simulation.threshold.warning_distance_pct:.0f}% · "
                f"🚨 {self.simulation.threshold.critical_distance_pct:.0f}% · "
                f"🆘 {self.simulation.threshold.emergency_distance_pct:.0f}%."
            )

        elif kind == "status" and data["state"] in ("finished", "stopped"):
            headline = (self.simulation.summary or {}).get("headline")
            text = f"⏹ **Simulation {data['state']}.**"
            if headline:
                text += f"\n\n🧠 {headline}"
            await self._send(text + "\n\nUse /risk for the portfolio overview.")
