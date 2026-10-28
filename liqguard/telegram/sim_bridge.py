"""Forwards one user's simulation events to that user's Telegram chat, and nowhere else."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from liqguard.alerts import (
    ACK_PREFIX,
    format_adl_message,
    format_prefunding_message,
    format_stop_message,
    wick_line,
)
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
        f"Distance to liq: **{card['distance_pct']:.1f}%**"
        + (f" (tier {card['threshold_pct']:.1f}%)" if card.get("threshold_pct") else ""),
        f"Mark: ${card['mark_price']:,.2f} · Liq: ${card['liquidation_price']:,.2f}",
    ]
    stress = wick_line(card.get("wicks"))
    if stress:
        lines.append(stress)
    if card.get("adaptive_reasons"):
        lines.append(f"Tiers widened: {'; '.join(card['adaptive_reasons'])}")
    lines += [
        "",
        f"🧠 _{muse}:_ {card['explanation']}",
    ]
    if card.get("margin_to_add_usd"):
        where = "to the cross wallet" if card.get("margin_mode") == "cross" else "margin"
        lines += [
            "",
            f"💰 Add **${card['margin_to_add_usd']:,.2f}** {where} → {card['target_distance_pct']:.0f}% from liq",
            f"✂️ Or reduce size by **{card['reduce_size_pct']:.1f}%**",
        ]
    rescue, explanation = card.get("rescue"), card.get("rescue_explanation")
    if rescue:
        best = next((o for o in rescue["options"] if o["kind"] == rescue["recommended"]), None)
        if best:
            lines += ["", f"🛟 Cheapest rescue: **{best['title']}** (${best['costs']['total_cost_usd']:,.2f} incl. fees + 24h funding)"]
        if explanation and explanation.get("headline"):
            lines.append(f"_{explanation['headline']}_")
    return "\n".join(lines)


def ack_row(ack_id: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="✅ I'm awake", callback_data=f"{ACK_PREFIX}{ack_id}")]


def advice_keyboard(card: dict) -> InlineKeyboardMarkup:
    rows = [ack_row(card["ack_id"])] if card.get("ack_id") else []
    rows.append([InlineKeyboardButton(text="🛟 Compare rescue options", callback_data=f"rescue:sim:{card['symbol']}")])
    rescue = card.get("rescue")
    if rescue:
        labels = {"add_margin": "💰 Add margin", "reduce": "✂️ Reduce", "hedge": "🛡 Hedge"}
        rows.append([
            InlineKeyboardButton(
                text=labels.get(o["kind"], o["kind"]) + (" ⭐" if o["kind"] == rescue["recommended"] else ""),
                callback_data=f"rescue:sim:{card['symbol']}:{o['kind']}",
            )
            for o in rescue["options"]
        ])
    if card.get("margin_to_add_usd"):
        rows.append([
            InlineKeyboardButton(
                text=f"✅ Propose adding ${card['margin_to_add_usd']:,.0f}",
                callback_data=f"action:propose:{card['symbol']}",
            ),
            InlineKeyboardButton(text="❌ Dismiss", callback_data="action:dismiss"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
            await self._send(format_advice_message(data), advice_keyboard(data))

        elif kind == "alert" and data["level"] == "funding":
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🛟 Rescue options", callback_data=f"rescue:sim:{data['symbol']}"),
            ]])
            await self._send(format_prefunding_message(data["funding"], demo=True), keyboard)

        elif kind == "alert" and data["level"] == "stop":
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🛟 Rescue options", callback_data=f"rescue:sim:{data['symbol']}"),
            ]])
            await self._send(format_stop_message(data["stop"], data.get("explanation"), demo=True), keyboard)

        elif kind == "alert" and data["level"] == "adl":
            await self._send(format_adl_message(data["symbol"], data["venue"], data["side"], data["adl"],
                                                data.get("unrealized_pnl", 0.0), demo=True))

        elif kind == "escalation":
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                ack_row(data["ack_id"]),
                [InlineKeyboardButton(text="🛟 Rescue options", callback_data=f"rescue:sim:{data['symbol']}")],
            ])
            await self._send(data["text"], keyboard)

        elif kind == "ack" and data.get("source") != "telegram":
            await self._send(f"✅ {data['symbol']} alert acknowledged on the {data['source']} — reminders stopped.")

        elif kind == "audit" and data["event"] == "simulation_started":
            t = self.simulation.threshold
            session = self.simulation._session_dict()
            esc = (f"⏰ Critical/emergency alerts repeat every {t.escalation_interval_minutes:g} demo min "
                   f"(×{self.simulation.escalation_seconds_per_minute:g}s) until you tap ✅ I'm awake, "
                   f"up to {t.escalation_max_reminders}." if t.escalation_enabled else "⏰ Wake-up escalation is off (/settings).")
            await self._send(
                "🎬 **Market simulation started** (demo data)\n\n"
                "Prices will walk toward liquidation. Your tiers: "
                f"⚠️ {t.warning_distance_pct:.0f}% · 🚨 {t.critical_distance_pct:.0f}% · "
                f"🆘 {t.emergency_distance_pct:.0f}%"
                + (f", widened per symbol for 30-day wicks and the session (demo clock {session['weekday']}: {'; '.join(session['reasons'])})"
                   if t.adaptive_thresholds and session["reasons"] else "")
                + ".\n"
                "Liquidation prices are computed for cross margin (BTC + SOL share one Binance wallet); "
                "demo funding settles on a simulated clock (12 min per step) and ⏳ warns before large payments.\n"
                "🛑 Stop-losses are checked against liquidation, ⚡ ADL lights are watched, and " + esc
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

    async def notify(self, user_id: int, text: str, actions: Optional[list[tuple[str, str]]] = None) -> None:
        keyboard = None
        if actions:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=label, callback_data=data) for label, data in actions]
            ])
        try:
            await self.bot.send_message(user_id, text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            logger.error("Failed to deliver alert to %s: %s", user_id, type(e).__name__)
