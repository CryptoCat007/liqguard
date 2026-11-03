"""
Forwards one user's simulation events to that user's Telegram chat, and nowhere
else. Every message is written in the owner's language (`simulation.language`)
and its keyboard is built for that same language, so text and buttons always
match; every message carries ⏹ Stop demo.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Callable, Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from liqguard.alerts import (
    ACK_PREFIX,
    comparison_line,
    format_adl_message,
    format_correlation_message,
    format_prefunding_message,
    format_sizing_message,
    format_stop_message,
    format_triage_message,
    priority_line,
    wick_line,
)
from liqguard.alerts.messages import (
    advice_recommendation,
    recommendation_line,
    side_upper,
    venue_name,
)
from liqguard.i18n import EN, join, t, tr
from liqguard.simulation import SCENARIOS, MarketSimulation

logger = logging.getLogger(__name__)

LEVEL_EMOJI = {"warning": "⚠️", "critical": "🚨", "emergency": "🆘"}
STOP_DEMO = "democtl:stop"
SENT_MEMORY = 64


def format_advice_message(card: dict, lang: str = EN) -> str:
    """Telegram text for one simulated alert + its Muse advice card."""
    level = card["level"]
    lines = [
        t(lang, "sim.alert_head", emoji=LEVEL_EMOJI.get(level, "❓"), level=t(lang, f"common.severity_upper.{level}"))
        + t(lang, "alert.demo_tag"),
        "",
        t(lang, "alert.position_on", symbol=card["symbol"], side=side_upper(lang, card["side"]), venue=venue_name(lang, card["venue"])),
        t(lang, "sim.distance", dist=f"{card['distance_pct']:.1f}")
        + (t(lang, "sim.tier", tier=f"{card['threshold_pct']:.1f}") if card.get("threshold_pct") else ""),
        t(lang, "sim.mark_liq", mark=f"{card['mark_price']:,.2f}", liq=f"{card['liquidation_price']:,.2f}"),
    ]
    rec = advice_recommendation(card)
    lines.append(recommendation_line(lang, rec["text"], rec.get("source", rec["live"])))
    for line in (priority_line(card.get("priority"), card.get("handle_first"), lang),
                 comparison_line(card["liq_comparison"], lang) if card.get("liq_comparison") else None,
                 wick_line(card.get("wicks"), lang)):
        if line:
            lines.append(line)
    if card.get("adaptive_reasons"):
        lines.append(t(lang, "sim.widened", reasons=join(lang, card["adaptive_reasons"], "; ")))
    rescue = card.get("rescue")
    leads_with_rescue = bool(rescue and not rescue.get("already_safe"))
    if card.get("margin_to_add_usd") and leads_with_rescue:
        where = t(lang, "sim.where_cross" if card.get("margin_mode") == "cross" else "sim.where_margin")
        lines += [
            "",
            t(lang, "sim.add", amount=f"{card['margin_to_add_usd']:,.2f}", where=where, target=f"{card['target_distance_pct']:.0f}"),
            t(lang, "sim.reduce", pct=f"{card['reduce_size_pct']:.1f}"),
        ]
    return "\n".join(lines)


def ack_row(ack_id: str, lang: str = EN) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=t(lang, "kb.alert_awake"), callback_data=f"{ACK_PREFIX}{ack_id}")]


def stop_row(lang: str = EN) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=t(lang, "kb.sim_stop"), callback_data=STOP_DEMO)]


def advice_keyboard(card: dict, lang: str = EN) -> InlineKeyboardMarkup:
    rows = [ack_row(card["ack_id"], lang)] if card.get("ack_id") else []
    rows.append([InlineKeyboardButton(text=t(lang, "sim.compare_rescue"), callback_data=f"rescue:sim:{card['symbol']}")])
    rescue = card.get("rescue")
    if rescue:
        rows.append([
            InlineKeyboardButton(
                text=t(lang, f"tools.option.{o['kind']}") + (" ⭐" if o["kind"] == rescue["recommended"] else ""),
                callback_data=f"rescue:sim:{card['symbol']}:{o['kind']}",
            )
            for o in rescue["options"]
        ])
    if card.get("margin_to_add_usd"):
        rows.append([
            InlineKeyboardButton(text=t(lang, "sim.propose", amount=f"{card['margin_to_add_usd']:,.0f}"),
                                 callback_data=f"action:propose:{card['symbol']}"),
            InlineKeyboardButton(text=t(lang, "sim.dismiss"), callback_data="action:dismiss"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


AlertMarkup = Callable[..., Any]


class TelegramSimulationBridge:
    """
    Subscribes to a user-owned MarketSimulation and sends its alerts to the
    owner's chat. The recipient is fixed to `simulation.owner_id`; there is
    no list of chats to broadcast to, and the public demo simulation (owner
    None) cannot be bridged.

    With `alert_markup` (the inline UI), keyboards lead with
    🚑 Handle this first / ✅ I'm awake / 😴 Mute this demo / ⏹ Stop demo,
    signed for the owner and labelled in the owner's language.
    Once the demo is muted (✅ / 😴 / Dismiss / ⏹) nothing more is sent.
    """

    def __init__(self, bot: Bot, simulation: MarketSimulation, alert_markup: Optional[AlertMarkup] = None):
        if simulation.owner_id is None:
            raise ValueError("Public simulations are never forwarded to Telegram")
        self.bot = bot
        self.simulation = simulation
        self.chat_id: int = simulation.owner_id
        self.alert_markup = alert_markup
        self._sent: "OrderedDict[str, tuple[int, Optional[InlineKeyboardMarkup]]]" = OrderedDict()
        self.unsubscribe: Callable[[], None] = simulation.subscribe("telegram", self.on_event)

    @property
    def lang(self) -> str:
        return self.simulation.language

    def keyboard(self, rows: Optional[list[list[InlineKeyboardButton]]] = None, symbol: Optional[str] = None,
                 ack_id: Optional[str] = None) -> InlineKeyboardMarkup:
        """`rows` plus the demo's UI buttons (rescue / ack / mute / stop), all in the owner's language."""
        lang, rows = self.lang, list(rows or [])
        legacy = ({f"rescue:sim:{symbol}"} if symbol else set()) | ({f"{ACK_PREFIX}{ack_id}"} if ack_id else set())
        top = None
        if self.alert_markup is not None:
            top = self.alert_markup(self.chat_id, [("", data) for data in sorted(legacy)], lang=lang, demo=True)
        if top is None:
            return InlineKeyboardMarkup(inline_keyboard=rows + [stop_row(lang)])
        rest = [row for row in rows if not any(b.callback_data in legacy for b in row)]
        return InlineKeyboardMarkup(inline_keyboard=list(top.inline_keyboard) + rest)

    async def _send(self, text: str, keyboard: Optional[InlineKeyboardMarkup] = None, stop_button: bool = True,
                    ref: Optional[str] = None) -> None:
        """`ref` (an advice or alert id) remembers the message so Muse's later text can replace it in place."""
        if self.simulation.muted:
            return
        if keyboard is None and stop_button:
            keyboard = self.keyboard()
        try:
            sent = await self.bot.send_message(self.chat_id, text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            logger.error("Failed to send simulation event to %s: %s", self.chat_id, type(e).__name__)
            return
        message_id = getattr(sent, "message_id", None)
        if ref and isinstance(message_id, int):
            self._sent[ref] = (message_id, keyboard)
            while len(self._sent) > SENT_MEMORY:
                self._sent.popitem(last=False)

    async def _edit(self, ref: str, text: str) -> None:
        """Muse's text arrived after the deterministic one was sent: update that message, keep its buttons."""
        if self.simulation.muted or ref not in self._sent:
            return
        message_id, keyboard = self._sent[ref]
        try:
            await self.bot.edit_message_text(text, chat_id=self.chat_id, message_id=message_id,
                                             parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            logger.info("Could not update simulation message for %s: %s", self.chat_id, type(e).__name__)

    def _rescue_rows(self, symbol: str) -> list[list[InlineKeyboardButton]]:
        return [[InlineKeyboardButton(text=t(self.lang, "alert.rescue_button"), callback_data=f"rescue:sim:{symbol}")]]

    async def on_event(self, event: dict) -> None:
        kind, data, lang = event["type"], event["data"], self.lang

        if kind == "advice":
            rows = advice_keyboard(data, lang).inline_keyboard
            await self._send(format_advice_message(data, lang), self.keyboard(rows, data["symbol"], data.get("ack_id")),
                             ref=data["id"])

        elif kind == "advice_update":
            await self._edit(data["id"], format_advice_message(data, lang))

        elif kind == "alert_update" and data["level"] == "stop":
            await self._edit(data["id"], format_stop_message(data["stop"], data.get("explanation"), demo=True, lang=lang))

        elif kind == "alert" and data["level"] == "funding":
            await self._send(format_prefunding_message(data["funding"], demo=True, lang=lang),
                             self.keyboard(self._rescue_rows(data["symbol"]), data["symbol"]))

        elif kind == "alert" and data["level"] == "stop":
            await self._send(format_stop_message(data["stop"], data.get("explanation"), demo=True, lang=lang),
                             self.keyboard(self._rescue_rows(data["symbol"]), data["symbol"]), ref=data["id"])

        elif kind == "alert" and data["level"] == "triage":
            rows = [ack_row(data["ack_id"], lang)] if data.get("ack_id") else []
            rows.append([InlineKeyboardButton(text=t(lang, "sim.rescue_first", symbol=data["symbol"]),
                                              callback_data=f"rescue:sim:{data['symbol']}")])
            await self._send(format_triage_message(data["items"], data.get("severity"), demo=True, focus=data.get("focus"), lang=lang),
                             self.keyboard(rows, data["symbol"], data.get("ack_id")))

        elif kind == "alert" and data["level"] == "sizing":
            await self._send(format_sizing_message(data["sizing"], demo=True, lang=lang))

        elif kind == "alert" and data["level"] == "correlation":
            await self._send(format_correlation_message(data.get("stress"), data["concentration"], data["comparisons"],
                                                        data.get("severity"), demo=True, lang=lang))

        elif kind == "alert" and data["level"] == "adl":
            await self._send(format_adl_message(data["symbol"], data["venue"], data["side"], data["adl"],
                                                data.get("unrealized_pnl", 0.0), demo=True, lang=lang))

        elif kind == "escalation":
            rows = [ack_row(data["ack_id"], lang), *self._rescue_rows(data["symbol"])]
            await self._send(data["text"], self.keyboard(rows, data["symbol"], data["ack_id"]))

        elif kind == "ack" and data.get("source") not in ("telegram",):
            await self.simulation_notice(t(lang, "sim.acked_elsewhere", symbol=data["symbol"],
                                           source=t(lang, f"sim.source.{data['source']}") if data["source"] in ("dashboard", "web") else data["source"]))

        elif kind == "audit" and data["event"] == "simulation_started":
            await self._send(self.started_text())

        elif kind == "status" and data["state"] == "finished":
            headline = (self.simulation.summary or {}).get("headline")
            text = t(lang, "sim.finished")
            if headline:
                text += f"\n\n🧠 {tr(lang, headline)}"
            await self._send(text + "\n\n" + t(lang, "sim.finished_hint"), stop_button=False)

    async def simulation_notice(self, text: str) -> None:
        """A one-line confirmation sent even though the demo has just been muted (the mute came from another channel)."""
        try:
            await self.bot.send_message(self.chat_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.error("Failed to send simulation notice to %s: %s", self.chat_id, type(e).__name__)

    def started_text(self) -> str:
        sim, lang = self.simulation, self.lang
        th = sim.threshold
        session = sim._session_dict()
        esc = (t(lang, "sim.esc_on", interval=f"{th.escalation_interval_minutes:g}", seconds=f"{sim.escalation_seconds_per_minute:g}",
                 max=th.escalation_max_reminders) if th.escalation_enabled else t(lang, "sim.esc_off"))
        scenario = SCENARIOS.get(sim.scenario)
        name = t(lang, f"demo.scenario.{sim.scenario}") if scenario else ""
        widened = (t(lang, "sim.widened_session", weekday=session["weekday"], reasons=join(lang, session["reasons"], "; "))
                   if th.adaptive_thresholds and session["reasons"] else "")
        return (t(lang, "sim.started", name=name, steps=sim.steps, seconds=f"{sim.steps * sim.tick_interval:.0f}") + "\n\n"
                + t(lang, "sim.started_tiers", warning=f"{th.warning_distance_pct:.0f}", critical=f"{th.critical_distance_pct:.0f}",
                    emergency=f"{th.emergency_distance_pct:.0f}", widened=widened) + "\n"
                + t(lang, "sim.started_body") + " " + esc + "\n\n" + t(lang, "sim.started_stop"))


def attach_bridges(bot: Bot, alert_markup: Optional[AlertMarkup] = None) -> Callable[[MarketSimulation], Callable[[], None]]:
    """Registry hook: give every user simulation a bridge to its owner's chat."""
    def hook(simulation: MarketSimulation) -> Callable[[], None]:
        return TelegramSimulationBridge(bot, simulation, alert_markup).unsubscribe
    return hook


class TelegramNotifier:
    """`alerts.Notifier` implementation: one message to one user, buttons in the same language as the text."""

    def __init__(self, bot: Bot, alert_markup: Optional[AlertMarkup] = None):
        self.bot = bot
        self.alert_markup = alert_markup

    async def notify(self, user_id: int, text: str, actions: Optional[list[tuple[str, str]]] = None, lang: str = EN,
                     demo: bool = False) -> Optional[Any]:
        """Every demo message (alerts, info warnings and reminders alike) carries ⏹ Stop demo. Returns the sent message."""
        actions = list(actions or [])
        keyboard = None
        if (actions or demo) and self.alert_markup is not None:
            keyboard = self.alert_markup(user_id, actions, lang=lang, demo=demo)
        if (actions or demo) and keyboard is None:
            rows = [[InlineKeyboardButton(text=label, callback_data=data) for label, data in actions]] if actions else []
            if demo:
                rows.append([InlineKeyboardButton(text=t(lang, "kb.sim_stop"), callback_data=STOP_DEMO)])
            keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        try:
            return await self.bot.send_message(user_id, text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            logger.error("Failed to deliver alert to %s: %s", user_id, type(e).__name__)
            return None
