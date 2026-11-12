"""The production Telegram wiring, shared by `main.py`, the real-dispatcher tests and the pre-release check."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from liqguard.alerts import EscalationService, PositionMonitor
from liqguard.core import AppContext

from .callback_log import CallbackLogMiddleware
from .handlers import BotHandlers
from .sim_bridge import TelegramNotifier, attach_bridges
from .ui import TelegramUI, build_ui


@dataclass
class BotApp:
    dispatcher: Dispatcher
    ui: TelegramUI
    notifier: TelegramNotifier
    escalation: EscalationService
    monitor: PositionMonitor


def wire_bot(ctx: AppContext, bot: Bot, escalation_check_seconds: float = 5.0) -> BotApp:
    ui = build_ui(ctx)
    ctx.ui = ui
    ctx.simulations.on_create(attach_bridges(bot, ui.alert_markup))

    async def billing_notify(telegram_id: int, kind: str, resource_id: str) -> None:
        if kind != "rescue_pass_purchased":
            return
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        from liqguard.i18n import Translator
        from liqguard.payments.entitlements import format_taipei_hm

        user = await ctx.storage.get_user(telegram_id)
        lang = user.language if user is not None else "en"
        T = Translator(lang)
        until_hm = "--:--"
        if ctx.entitlements is not None:
            status = await ctx.entitlements.get_status(telegram_id)
            if status.rescue_until is not None:
                until_hm = format_taipei_hm(status.rescue_until)
        text = T("pro.notifications.rescue_mode_active", until=until_hm)
        sign = ui.signer(telegram_id)
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=T("kb.risk_overview"), callback_data=sign("ro")),
            InlineKeyboardButton(text=T("kb.ask"), callback_data=sign("qa")),
        ]])
        try:
            await bot.send_message(telegram_id, text, reply_markup=markup)
        except Exception:
            pass

    ctx.billing_notify = billing_notify

    dp = Dispatcher(storage=MemoryStorage())
    dp.callback_query.outer_middleware(CallbackLogMiddleware(ui.codec))
    ui.register(dp)
    BotHandlers(ctx).register(dp, bot)

    notifier = TelegramNotifier(bot, ui.alert_markup)
    escalation = EscalationService(notifier, ctx.escalations, check_seconds=escalation_check_seconds)
    monitor = PositionMonitor(
        users=ctx.storage.get_all_users_with_bindings,
        positions=ctx.positions.snapshot_for,
        notifier=notifier,
        alert_manager=ctx.alerts,
        explainer=ctx.muse,
        poll_interval=ctx.settings.poll_interval,
        prefunding_lead_minutes=ctx.settings.prefunding_lead_minutes,
        escalation=escalation,
        demo_window=ctx.demo_alerts,
        entitlements=ctx.entitlements,
    )
    return BotApp(dp, ui, notifier, escalation, monitor)
