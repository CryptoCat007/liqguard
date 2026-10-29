"""Telegram bot handlers. Thin adapters: every handler resolves the caller's own user id and delegates to `AppContext`."""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from liqguard.ai import get_news_stub
from liqguard.alerts import ACK_PREFIX
from liqguard.core import AppContext, BookSnapshot
from liqguard.core.positions import LABEL_SIMULATION
from liqguard.core.user_settings import apply_setting as apply_user_setting
from liqguard.exchanges import SCENARIOS, BinanceAdapter, HyperliquidAdapter
from liqguard.models import MAINNET, TESTNET, ExchangeBinding, Position, Side, User
from liqguard.risk import (
    AdviceCalculator,
    PortfolioAnalyzer,
    RescuePlan,
    RescueSimulator,
    RiskEngine,
    format_portfolio_overview_text,
    portfolio_insights,
    position_protection,
)
from liqguard.simulation import SCENARIOS as SIM_SCENARIOS
from liqguard.simulation import MarketSimulation

from .views import (
    format_option_detail,
    format_rescue_text,
    funding_lines,
    insight_lines,
    insights_section,
    liquidation_lines,
    protection_lines,
    protection_summary,
    rescue_keyboard,
)

logger = logging.getLogger(__name__)

RISK_EMOJI = {"SAFE": "✅", "WARNING": "⚠️", "CRITICAL": "🚨", "EMERGENCY": "🆘"}
NO_EXCHANGES = "No exchanges connected.\n\nUse /bind to connect an exchange or /demo to try demo mode."
GENERIC_ERROR = "❌ Something went wrong. Please try again."


class BindingStates(StatesGroup):
    waiting_for_binance_api_key = State()
    waiting_for_binance_api_secret = State()
    waiting_for_hyperliquid_address = State()


class ThresholdStates(StatesGroup):
    waiting_for_value = State()


class BotHandlers:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.storage = ctx.storage
        self.llm = ctx.llm
        self.actions_manager = ctx.actions
        self.positions = ctx.positions
        self.simulations = ctx.simulations
        self.router = Router()
        self._pending_screenshots: dict[int, dict] = {}
        self._bot: Optional[Bot] = None

    async def get_or_create_user(self, message: Message) -> User:
        user = await self.storage.get_user(message.from_user.id)
        if not user:
            user = User(telegram_id=message.from_user.id, username=message.from_user.username, threshold=self.ctx.settings.default_threshold())
            await self.storage.save_user(user)
        return user

    def simulation_for(self, user: User) -> MarketSimulation:
        sim = self.simulations.get_or_create(user.telegram_id)
        if not sim.is_running:
            sim.configure(user.threshold, user.language)
        return sim

    def register(self, dp: Dispatcher, bot: Bot) -> None:
        self._bot = bot
        r = self.router
        dp.include_router(r)

        r.message.register(self.cmd_start, CommandStart())
        r.message.register(self.cmd_help, Command("help"))
        r.message.register(self.cmd_bind, Command("bind"))
        r.message.register(self.cmd_unbind, Command("unbind"))
        r.message.register(self.cmd_positions, Command("positions"))
        r.message.register(self.cmd_threshold, Command("threshold"))
        r.message.register(self.cmd_demo, Command("demo"))
        r.message.register(self.cmd_ask, Command("ask"))
        r.message.register(self.cmd_language, Command("language", "lang"))
        r.message.register(self.cmd_status, Command("status"))
        r.message.register(self.cmd_risk, Command("risk"))
        r.message.register(self.cmd_actions, Command("actions"))
        r.message.register(self.cmd_killswitch, Command("killswitch"))
        r.message.register(self.cmd_dashboard, Command("dashboard"))
        r.message.register(self.cmd_settings, Command("settings"))

        r.callback_query.register(self.callback_venue_selected, F.data.startswith("venue:"))
        r.callback_query.register(self.callback_unbind_venue, F.data.startswith("unbind:"))
        r.callback_query.register(self.callback_demo_scenario, F.data.startswith("demo:"))
        r.callback_query.register(self.callback_threshold_type, F.data.startswith("threshold:"))
        r.callback_query.register(self.callback_language, F.data.startswith("lang:"))
        r.callback_query.register(self.callback_screenshot_confirm, F.data.startswith("screenshot:"))
        r.callback_query.register(self.callback_action_confirm, F.data.startswith("action:"))
        r.callback_query.register(self.callback_demo_control, F.data.startswith("democtl:"))
        r.callback_query.register(self.callback_risk, F.data.startswith("risk:"))
        r.callback_query.register(self.callback_rescue, F.data.startswith("rescue:"))
        r.callback_query.register(self.callback_ack, F.data.startswith(ACK_PREFIX))
        r.callback_query.register(self.callback_settings, F.data.startswith("settings:"))

        r.message.register(self.process_binance_api_key, BindingStates.waiting_for_binance_api_key)
        r.message.register(self.process_binance_api_secret, BindingStates.waiting_for_binance_api_secret)
        r.message.register(self.process_hyperliquid_address, BindingStates.waiting_for_hyperliquid_address)
        r.message.register(self.process_threshold_value, ThresholdStates.waiting_for_value)

        r.message.register(self.handle_photo, F.photo)

    # ------------------------------------------------------------------ basics

    async def cmd_start(self, message: Message) -> None:
        await self.get_or_create_user(message)
        await message.answer(
            "🛡️ **Welcome to LiqGuard!**\n\n"
            "I watch your perpetual futures positions and warn you before liquidation.\n\n"
            "**Supported exchanges**\n"
            "• Binance USDⓈ-M Futures (read-only API key)\n"
            "• Hyperliquid (wallet address only)\n\n"
            "**Quick start**\n"
            "1. /bind to connect an exchange\n"
            "2. /positions or /risk to see your risk\n"
            "3. /threshold to set alert levels, /settings for wake-up reminders\n"
            "4. /demo to try it with simulated positions\n"
            "5. /dashboard for a private web dashboard link\n\n"
            "Type /help for all commands.",
            parse_mode="Markdown",
        )

    async def cmd_help(self, message: Message) -> None:
        user = await self.get_or_create_user(message)
        if user.language == "zh-TW":
            text = (
                "🛡️ **LiqGuard 指令**\n\n"
                "**連接交易所**\n/bind - 連接交易所\n/unbind - 移除連接\n\n"
                "**監控倉位**\n/positions - 倉位與風險\n/risk - 投資組合風險總覽\n/status - 帳戶狀態\n\n"
                "**設定**\n/threshold - 警報閾值\n/settings - 喚醒提醒、自適應閾值、止損檢查、倉位大小\n/language - 切換語言\n/demo - 演示模式\n\n"
                "**AI 功能**\n/ask <問題> - 詢問倉位\n📸 發送截圖 - 解析倉位\n\n"
                "**網頁儀表板**\n/dashboard - 取得一次性登入連結\n/dashboard revoke - 登出所有裝置\n\n"
                "**保護操作（僅測試網）**\n/actions - 待處理操作\n/killswitch - 緊急停止\n\n"
                "**安全說明**\n• 主網僅接受只讀 API 金鑰\n• 每位用戶的金鑰以獨立密鑰加密\n• 執行操作僅限測試網"
            )
        else:
            text = (
                "🛡️ **LiqGuard Commands**\n\n"
                "**Connect exchanges**\n/bind - Connect Binance or Hyperliquid\n/unbind - Remove a connection\n\n"
                "**Monitor**\n/positions - Positions with risk assessment\n/risk - Portfolio risk: what to handle first, stress test, sizing\n"
                "/status - Account status\n\n"
                "**Settings**\n/threshold - Alert thresholds\n/settings - Wake-up reminders, adaptive tiers, stop-loss checks, position sizing\n"
                "/language - Change language\n/demo - Demo mode\n\n"
                "**AI**\n/ask <question> - Ask about your positions\n📸 Send a screenshot - Parse a position\n\n"
                "**Web dashboard**\n/dashboard - One-time login link\n/dashboard revoke - Sign out everywhere\n\n"
                "**Protective actions (testnet only)**\n/actions - Pending actions\n/killswitch - Emergency stop\n\n"
                "**Security**\n• Mainnet: read-only API keys only\n• Each user's keys are encrypted with their own data key\n"
                "• Execution only on testnet"
            )
        await message.answer(text, parse_mode="Markdown")

    # ------------------------------------------------------------------ binding

    async def cmd_bind(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟡 Binance Futures (read-only)", callback_data="venue:binance")],
            [InlineKeyboardButton(text="🟢 Hyperliquid", callback_data="venue:hyperliquid")],
            [InlineKeyboardButton(text="🧪 Binance testnet (protective actions)", callback_data="venue:binance_testnet")],
        ])
        await message.answer(
            "Select the exchange to connect:\n\n"
            "• **Binance**: read-only API key (keys with trade/withdraw permission are rejected)\n"
            "• **Hyperliquid**: wallet address only\n"
            "• **Binance testnet**: trading key for tier-1 protective actions on testnet",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    async def callback_venue_selected(self, callback: CallbackQuery, state: FSMContext) -> None:
        venue = callback.data.split(":", 1)[1]
        if venue in ("binance", "binance_testnet"):
            network = TESTNET if venue == "binance_testnet" else MAINNET
            await state.set_state(BindingStates.waiting_for_binance_api_key)
            await state.update_data(network=network)
            if network == TESTNET:
                text = (
                    "🧪 **Binance Futures testnet**\n\n"
                    "Create a key at testnet.binancefuture.com. It may trade (testnet only); "
                    "LiqGuard uses it solely for confirmed add-margin actions.\n\nSend the API key now:"
                )
            else:
                text = (
                    "🔑 **Binance API key setup**\n\n"
                    "Use a **READ-ONLY** key:\n• ✅ Enable Reading\n• ❌ Futures / Spot & Margin trading\n"
                    "• ❌ Withdrawals and transfers\n• IP restriction recommended\n\n"
                    "Keys with trade or withdraw permission are rejected.\n\nSend your API key now:"
                )
            await callback.message.edit_text(text, parse_mode="Markdown")
        elif venue == "hyperliquid":
            await state.set_state(BindingStates.waiting_for_hyperliquid_address)
            await callback.message.edit_text(
                "🔗 **Hyperliquid wallet setup**\n\nSend your wallet address (0x...).\n\n"
                "✅ No key needed — only public data is read.",
                parse_mode="Markdown",
            )
        await callback.answer()

    async def process_binance_api_key(self, message: Message, state: FSMContext) -> None:
        api_key = (message.text or "").strip()
        await self._delete_quietly(message)
        if len(api_key) < 20:
            await message.answer("❌ That doesn't look like an API key. Please try again.")
            return
        await state.update_data(api_key=api_key)
        await state.set_state(BindingStates.waiting_for_binance_api_secret)
        await message.answer("✅ API key received (message deleted).\n\nNow send your API secret:")

    async def process_binance_api_secret(self, message: Message, state: FSMContext) -> None:
        api_secret = (message.text or "").strip()
        await self._delete_quietly(message)
        data = await state.get_data()
        await state.clear()
        api_key, network = data.get("api_key"), data.get("network", MAINNET)
        if not api_key:
            await message.answer("❌ Session expired. Please start over with /bind")
            return

        status_msg = await message.answer("🔄 Checking the key's permissions with Binance...")
        try:
            async with BinanceAdapter(api_key, api_secret, network=network) as adapter:
                check = await adapter.check_key()
        except Exception as e:
            logger.error("Binance key check failed for user %s: %s", message.from_user.id, type(e).__name__)
            await status_msg.edit_text(GENERIC_ERROR)
            return

        if not check.accepted:
            await status_msg.edit_text(f"🚫 {check.message}\n\nUse /bind to try again.")
            return

        user = await self.get_or_create_user(message)
        binding = ExchangeBinding(
            venue="binance",
            binding_type="api_key",
            network=network,
            api_key=api_key,
            api_secret=api_secret,
            permissions=check.permissions,
        )
        await self.storage.save_binding(user.telegram_id, binding)
        label = "Binance testnet" if network == TESTNET else "Binance"
        await status_msg.edit_text(
            f"✅ **{label} connected** (key {binding.api_key_hint})\n\n{check.message}\n"
            f"Permissions: {', '.join(check.permissions) or 'none'}\n\nUse /positions to view your positions.",
            parse_mode="Markdown",
        )

    async def process_hyperliquid_address(self, message: Message, state: FSMContext) -> None:
        address = (message.text or "").strip().lower()
        if not re.match(r"^0x[a-f0-9]{40}$", address):
            await message.answer("❌ Invalid wallet address. It must be 0x followed by 40 hex characters.")
            return
        await state.clear()
        status_msg = await message.answer("🔄 Validating wallet address...")
        async with HyperliquidAdapter(address) as adapter:
            check = await adapter.check_key()
        if not check.accepted:
            await status_msg.edit_text(f"❌ {check.message}")
            return
        user = await self.get_or_create_user(message)
        await self.storage.save_binding(user.telegram_id, ExchangeBinding(
            venue="hyperliquid", binding_type="wallet", wallet_address=address, permissions=check.permissions,
        ))
        await status_msg.edit_text(
            f"✅ **Hyperliquid wallet connected!**\n\nAddress: `{address[:8]}...{address[-6:]}`\n\n"
            "Use /positions to view your positions.",
            parse_mode="Markdown",
        )

    @staticmethod
    async def _delete_quietly(message: Message) -> None:
        try:
            await message.delete()
        except Exception:
            pass

    async def cmd_unbind(self, message: Message) -> None:
        user = await self.get_or_create_user(message)
        buttons = []
        for b in user.bindings:
            if b.is_active:
                label = f"{'🧪' if b.is_testnet else '🟡' if b.venue == 'binance' else '🟢'} {b.venue.title()}"
                label += " testnet" if b.is_testnet else ""
                buttons.append([InlineKeyboardButton(text=label, callback_data=f"unbind:{b.venue}:{b.network}")])
        if not buttons:
            await message.answer("You have no connected exchanges.")
            return
        await message.answer("Select exchange to disconnect:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    async def callback_unbind_venue(self, callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        venue = parts[1]
        network = parts[2] if len(parts) > 2 else None
        success = await self.storage.delete_binding(callback.from_user.id, venue, network)
        await callback.message.edit_text(f"✅ {venue.title()} disconnected." if success else f"❌ No {venue} connection found.")
        await callback.answer()

    # ------------------------------------------------------------------ positions & risk

    async def _get_positions_for_user(self, user: User) -> tuple[list[Position], str]:
        return await self.positions.positions_for(user)

    async def _snapshot(self, user: User) -> BookSnapshot:
        return await self.positions.snapshot_for(user)

    @staticmethod
    def _assess(user: User, snapshot: BookSnapshot) -> list[tuple]:
        """(assessment, protection dict) per position, riskiest first, each assessed against its adaptive tiers."""
        out = []
        for pos in snapshot.positions:
            protection = position_protection(pos, snapshot.context, user.threshold)
            assessment = RiskEngine(protection.adaptive.threshold, snapshot.context).assess_position(pos)
            out.append((assessment, protection.to_dict()))
        return sorted(out, key=lambda x: x[0].distance_to_liquidation_pct)

    def _positions_text(self, user: User, snapshot: BookSnapshot) -> tuple[str, list]:
        context = snapshot.context
        assessed = self._assess(user, snapshot)
        insights = portfolio_insights(snapshot.positions, context, user.threshold)
        assessments = [a for a, _ in assessed]
        response = f"**{snapshot.label}**\n\n"
        for a, info in assessed:
            pos = a.position
            mode = f" · {pos.margin_mode}" if pos.venue.value != "demo" else ""
            response += (
                f"{RISK_EMOJI.get(a.risk_level_str, '❓')} **{pos.symbol}** "
                f"{'📈' if pos.side == Side.LONG else '📉'} {pos.side.value.upper()}"
                f"{' · testnet' if pos.network == TESTNET else ''}{mode}\n"
                f"├ Size: {pos.size}\n├ Entry: ${pos.entry_price:,.2f}\n├ Mark: ${pos.mark_price:,.2f}\n"
            )
            response += "\n".join(liquidation_lines(pos, context)) + "\n"
            response += f"├ Distance: **{a.distance_to_liquidation_pct:.1f}%**\n"
            response += "".join(line + "\n" for line in funding_lines(pos, context))
            response += f"├ PnL: ${pos.unrealized_pnl:,.2f}\n├ Leverage: {pos.leverage}x\n"
            response += "".join(line + "\n" for line in protection_lines(info))
            response += "".join(line + "\n" for line in insight_lines(insights, pos))
            if a.margin_to_safety > 0:
                response += (
                    f"└ Add **${a.margin_to_safety:,.2f}** margin → {user.threshold.safety_target_pct:.0f}% from liq "
                    "(or tap 🛟 to compare reduce / hedge)\n\n"
                )
            else:
                response += "└ Position is safe\n\n"
        if len(response) > 4000:
            response = response[:3900] + "\n\n... (truncated)"
        return response, assessments

    async def cmd_positions(self, message: Message) -> None:
        user = await self.get_or_create_user(message)
        snapshot = await self._snapshot(user)
        if not snapshot.label:
            await message.answer(NO_EXCHANGES)
            return
        if not snapshot.positions:
            await message.answer(f"{snapshot.label}\n\nNo open positions found.")
            return
        text, assessments = self._positions_text(user, snapshot)
        buttons = []
        for a in assessments[:3]:
            if a.severity is None:
                continue
            row = [InlineKeyboardButton(
                text=f"🛟 Rescue {a.position.symbol}", callback_data=self._rescue_callback(snapshot, a.position),
            )]
            if self.actions_manager.is_enabled() and a.margin_to_safety > 0:
                row.append(InlineKeyboardButton(
                    text=f"💰 Add margin to {a.position.symbol}",
                    callback_data=f"action:propose:{a.position.symbol}",
                ))
            buttons.append(row)
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)

    @staticmethod
    def _rescue_callback(snapshot: BookSnapshot, position: Position, kind: Optional[str] = None) -> str:
        source = "sim" if snapshot.label == LABEL_SIMULATION else position.venue.value
        return f"rescue:{source}:{position.symbol}" + (f":{kind}" if kind else "")

    async def _risk_overview_text(self, overview, language: str, focus: Optional[dict] = None) -> str:
        summary = await self.llm.summarize_portfolio(overview, language, focus)
        tag = "Muse (mock)" if summary.get("mock") else "Muse"
        return f"🧠 _{tag}:_ {summary['headline']}\n\n" + format_portfolio_overview_text(overview, language)

    async def _risk_reply(self, user: User) -> Optional[tuple[str, InlineKeyboardMarkup]]:
        snapshot = await self._snapshot(user)
        if not snapshot.positions:
            return None
        overview = PortfolioAnalyzer(user.threshold, snapshot.context).analyze_portfolio(snapshot.positions)
        insights = portfolio_insights(snapshot.positions, snapshot.context, user.threshold)
        focus = insights.focus
        text = await self._risk_overview_text(overview, user.language, focus)
        buttons = []
        riskiest = next((p for p in overview.position_summaries
                         if focus and p.symbol == focus["symbol"] and p.venue == focus["venue"]), overview.riskiest_position)
        infos = [position_protection(p, snapshot.context, user.threshold).to_dict() for p in snapshot.positions]
        section = insights_section(insights)
        if section:
            text += f"\n\n{section}"
        summary = protection_summary(infos)
        if summary:
            text += f"\n\n**Protection checks**\n{summary}"
        if riskiest:
            pos = next((p for p in snapshot.positions if p.symbol == riskiest.symbol and p.venue.value == riskiest.venue), None)
            if pos is not None:
                info = next(i for i, p in zip(infos, snapshot.positions) if p is pos)
                extra = liquidation_lines(pos, snapshot.context) + funding_lines(pos, snapshot.context) + protection_lines(info)
                text += f"\n\n**{pos.symbol} liquidation & funding · stress test**\n" + "\n".join(extra)
                if riskiest.severity is not None:
                    buttons.append([InlineKeyboardButton(
                        text=f"🛟 Rescue options for {riskiest.symbol}", callback_data=self._rescue_callback(snapshot, pos),
                    )])
        top = insights.top
        if top is not None and (riskiest is None or top.symbol != riskiest.symbol):
            pos = next((p for p in snapshot.positions if p.symbol == top.symbol and p.venue.value == top.venue), None)
            if pos is not None:
                buttons.insert(0, [InlineKeyboardButton(
                    text=f"🎯 Rescue {top.symbol} first", callback_data=self._rescue_callback(snapshot, pos),
                )])
        if riskiest and riskiest.margin_advice:
            buttons.append([InlineKeyboardButton(
                text=f"💰 Add ${riskiest.margin_advice.margin_to_add_usd:.0f} to {riskiest.symbol}",
                callback_data=f"action:propose:{riskiest.symbol}",
            )])
        buttons.append([
            InlineKeyboardButton(text="📋 Detailed Positions", callback_data="risk:positions"),
            InlineKeyboardButton(text="🔄 Refresh", callback_data="risk:refresh"),
        ])
        if len(text) > 4000:
            text = text[:3900] + "\n\n... (truncated — see /dashboard for everything)"
        return text, InlineKeyboardMarkup(inline_keyboard=buttons)

    async def _rescue_plan_for(self, user: User, source: str, symbol: str) -> Optional[RescuePlan]:
        """The caller's own position only: their simulation, demo book or exchange bindings."""
        if source == "sim":
            sim = self.simulations.get(user.telegram_id)
            return sim.build_rescue_plan(symbol) if sim is not None else None
        snapshot = await self._snapshot(user)
        position = next((p for p in snapshot.positions if p.symbol == symbol and p.venue.value == source), None)
        if position is None:
            return None
        ctx = snapshot.context
        simulator = RescueSimulator(ctx.fees, user.threshold.safety_target_pct)
        return simulator.simulate(position, ctx.account_for(position), ctx.funding_for(position), ctx.now)

    async def callback_rescue(self, callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) < 3:
            await callback.answer()
            return
        source, symbol, kind = parts[1], parts[2], (parts[3] if len(parts) > 3 else None)
        user = await self.storage.get_user(callback.from_user.id)
        if not user:
            await callback.answer("User not found")
            return
        plan = await self._rescue_plan_for(user, source, symbol)
        if plan is None:
            await callback.answer("Position not found")
            return
        explanation = await self.llm.explain_rescue(plan, user.language)
        data = plan.to_dict()
        if kind:
            text = format_option_detail(data, kind, explanation)
            if text is None:
                await callback.answer("Unknown option")
                return
            await callback.message.answer(text, parse_mode="Markdown")
        else:
            await callback.message.answer(
                format_rescue_text(data, explanation, demo=source in ("sim", "demo")),
                parse_mode="Markdown",
                reply_markup=rescue_keyboard(source, data, self.actions_manager.is_enabled() and source not in ("sim", "demo")),
            )
        await callback.answer()

    async def cmd_risk(self, message: Message) -> None:
        user = await self.get_or_create_user(message)
        _, label = await self._get_positions_for_user(user)
        if not label:
            await message.answer(NO_EXCHANGES)
            return
        reply = await self._risk_reply(user)
        if reply is None:
            await message.answer(f"{label}\n\nNo open positions found.")
            return
        await message.answer(reply[0], parse_mode="Markdown", reply_markup=reply[1])

    async def callback_risk(self, callback: CallbackQuery) -> None:
        action = callback.data.split(":")[1]
        user = await self.storage.get_user(callback.from_user.id)
        if not user:
            await callback.answer("User not found")
            return
        if action == "refresh":
            reply = await self._risk_reply(user)
            if reply is None:
                await callback.answer("No positions found")
                return
            await callback.message.edit_text(reply[0], parse_mode="Markdown", reply_markup=reply[1])
            await callback.answer("Refreshed")
        elif action == "positions":
            snapshot = await self._snapshot(user)
            if not snapshot.positions:
                await callback.answer("No positions found")
                return
            response = f"**{snapshot.label}**\n\n"
            for a, info in self._assess(user, snapshot):
                pos = a.position
                adl = info["adl"]
                lights = f" | ADL {adl['lights']}/5" if adl.get("lights") else ""
                response += (
                    f"{RISK_EMOJI.get(a.risk_level_str, '❓')} **{pos.symbol}** {pos.side.value.upper()}\n"
                    f"  Distance: {a.distance_to_liquidation_pct:.1f}% | PnL: ${pos.unrealized_pnl:,.2f}{lights}\n"
                )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📊 Back to Overview", callback_data="risk:refresh")],
            ])
            await callback.message.edit_text(response, parse_mode="Markdown", reply_markup=keyboard)
            await callback.answer()

    # ------------------------------------------------------------------ wake-up escalation & settings

    async def callback_ack(self, callback: CallbackQuery) -> None:
        """"I'm awake": stops reminders for the caller's own alert (live monitor or their simulation)."""
        ack_id = callback.data[len(ACK_PREFIX):]
        user_id = callback.from_user.id
        ack = self.ctx.escalations.acknowledge(user_id, ack_id, time.time())
        symbol = ack.symbol if ack else None
        if ack is None:
            sim = self.simulations.get(user_id)
            event = await sim.acknowledge(ack_id, source="telegram") if sim is not None else None
            symbol = event["symbol"] if event else None
        if symbol is None:
            await callback.answer("Already acknowledged or expired.")
            return
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(f"✅ Acknowledged — reminders for **{symbol}** stopped. Stay safe.", parse_mode="Markdown")
        await callback.answer("Reminders stopped")

    @staticmethod
    def _settings_text(user: User) -> str:
        t = user.threshold
        onoff = {True: "ON", False: "OFF"}
        return (
            "⚙️ **Settings**\n\n"
            f"⏰ Wake-up escalation: **{onoff[t.escalation_enabled]}**\n"
            f"   Critical/emergency alerts repeat every **{t.escalation_interval_minutes:g} min** until you tap "
            f"✅ I'm awake, up to **{t.escalation_max_reminders}** reminders, louder each time.\n"
            f"📈 Adaptive tiers: **{onoff[t.adaptive_thresholds]}** (widen for wicks, weekends, thin hours, closed stock market)\n"
            f"🛑 Stop-loss checks: **{onoff[t.stop_loss_checks]}**\n"
            f"📏 Position sizing: risk ≤ **{t.risk_per_trade_pct:g}%** of equity per trade (loss at the stop, else at "
            f"liquidation), margin ≤ **{t.max_margin_pct:g}%** of equity per position\n\n"
            "Text form: `/settings interval 3`, `/settings max 5`, `/settings escalation off`, "
            "`/settings adaptive on`, `/settings stops off`, `/settings risk 2`, `/settings maxmargin 25`\n\n"
            "💡 Make sure reminders can wake you: open this chat → bot name → Notifications → turn sound on "
            "(and add LiqGuard as a notification exception / allowed in Do Not Disturb). See the README."
        )

    @staticmethod
    def _settings_keyboard(user: User) -> InlineKeyboardMarkup:
        t = user.threshold

        def mark(cond: bool) -> str:
            return "• " if cond else ""

        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{mark(t.escalation_interval_minutes == m)}every {m}m", callback_data=f"settings:interval:{m}")
             for m in (1, 2, 5, 10)],
            [InlineKeyboardButton(text=f"{mark(t.escalation_max_reminders == n)}max {n}", callback_data=f"settings:max:{n}")
             for n in (3, 5, 10)],
            [InlineKeyboardButton(text=f"⏰ Escalation {'ON' if t.escalation_enabled else 'OFF'}", callback_data="settings:escalation:toggle"),
             InlineKeyboardButton(text=f"📈 Adaptive {'ON' if t.adaptive_thresholds else 'OFF'}", callback_data="settings:adaptive:toggle")],
            [InlineKeyboardButton(text=f"🛑 Stop checks {'ON' if t.stop_loss_checks else 'OFF'}", callback_data="settings:stops:toggle")],
            [InlineKeyboardButton(text=f"{mark(t.risk_per_trade_pct == r)}risk {r:g}%", callback_data=f"settings:risk:{r:g}")
             for r in (0.5, 1, 2, 5)],
            [InlineKeyboardButton(text=f"{mark(t.max_margin_pct == m)}margin ≤{m}%", callback_data=f"settings:maxmargin:{m}")
             for m in (10, 25, 50)],
        ])

    @staticmethod
    def apply_setting(threshold, key: str, value: str):
        """New `UserThreshold` with one setting changed, or None if the input is invalid."""
        return apply_user_setting(threshold, key, value)

    async def _save_settings(self, user: User, threshold) -> None:
        user.threshold = threshold
        await self.storage.update_threshold(user.telegram_id, threshold)
        sim = self.simulations.get(user.telegram_id)
        if sim is not None and not sim.is_running:
            sim.configure(threshold, user.language)

    async def cmd_settings(self, message: Message, command: Optional[CommandObject] = None) -> None:
        user = await self.get_or_create_user(message)
        args = (command.args if command else "") or ""
        parts = args.lower().split()
        if len(parts) == 2:
            updated = self.apply_setting(user.threshold, parts[0], parts[1])
            if updated is None:
                await message.answer("❌ Unknown or out-of-range setting. Interval 0.5–60 min, max 1–20, risk 0.1–20%, maxmargin 1–100%, on/off for toggles.")
                return
            await self._save_settings(user, updated)
        await message.answer(self._settings_text(user), parse_mode="Markdown", reply_markup=self._settings_keyboard(user))

    async def callback_settings(self, callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        user = await self.storage.get_user(callback.from_user.id)
        if user is None or len(parts) != 3:
            await callback.answer("Send /start first")
            return
        updated = self.apply_setting(user.threshold, parts[1], parts[2])
        if updated is None:
            await callback.answer("Invalid setting")
            return
        await self._save_settings(user, updated)
        try:
            await callback.message.edit_text(self._settings_text(user), parse_mode="Markdown", reply_markup=self._settings_keyboard(user))
        except Exception:
            pass
        await callback.answer("Saved")

    # ------------------------------------------------------------------ thresholds & language

    async def cmd_threshold(self, message: Message, state: FSMContext) -> None:
        user = await self.get_or_create_user(message)
        t = user.threshold
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⚠️ Warning ({t.warning_distance_pct}%)", callback_data="threshold:warning")],
            [InlineKeyboardButton(text=f"🚨 Critical ({t.critical_distance_pct}%)", callback_data="threshold:critical")],
            [InlineKeyboardButton(text=f"🆘 Emergency ({t.emergency_distance_pct}%)", callback_data="threshold:emergency")],
            [InlineKeyboardButton(text=f"🎯 Safety Target ({t.safety_target_pct}%)", callback_data="threshold:safety")],
        ])
        await message.answer(
            f"**Current Thresholds:**\n• Warning: {t.warning_distance_pct}%\n• Critical: {t.critical_distance_pct}%\n"
            f"• Emergency: {t.emergency_distance_pct}%\n• Safety target: {t.safety_target_pct}%\n"
            f"• Cooldown: {t.cooldown_seconds}s\n\nSelect a threshold to modify:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    async def callback_threshold_type(self, callback: CallbackQuery, state: FSMContext) -> None:
        threshold_type = callback.data.split(":")[1]
        await state.set_state(ThresholdStates.waiting_for_value)
        await state.update_data(threshold_type=threshold_type)
        labels = {
            "warning": "Warning threshold (alerts when distance falls below this %)",
            "critical": "Critical threshold (more urgent alerts)",
            "emergency": "Emergency threshold (highest urgency)",
            "safety": "Safety target (margin calculation target)",
        }
        await callback.message.edit_text(
            f"**Setting: {labels.get(threshold_type, threshold_type)}**\n\nEnter the new percentage value (e.g., 15):",
            parse_mode="Markdown",
        )
        await callback.answer()

    async def process_threshold_value(self, message: Message, state: FSMContext) -> None:
        try:
            value = float((message.text or "").strip().replace("%", ""))
            if value <= 0 or value > 100:
                raise ValueError
        except ValueError:
            await message.answer("❌ Please enter a number between 0 and 100.")
            return
        threshold_type = (await state.get_data()).get("threshold_type")
        user = await self.get_or_create_user(message)
        t = user.threshold
        attr = {
            "warning": "warning_distance_pct",
            "critical": "critical_distance_pct",
            "emergency": "emergency_distance_pct",
            "safety": "safety_target_pct",
        }.get(threshold_type)
        if attr:
            setattr(t, attr, value)
            await self.storage.update_threshold(user.telegram_id, t)
        await state.clear()
        await message.answer(f"✅ {str(threshold_type).title()} threshold updated to {value}%\n\nUse /threshold to view all settings.")

    async def cmd_language(self, message: Message) -> None:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🇺🇸 English", callback_data="lang:en")],
            [InlineKeyboardButton(text="🇹🇼 繁體中文", callback_data="lang:zh-TW")],
        ])
        await message.answer("Select your preferred language:", reply_markup=keyboard)

    async def callback_language(self, callback: CallbackQuery) -> None:
        lang = callback.data.split(":")[1]
        if lang not in ("en", "zh-TW"):
            await callback.answer()
            return
        user = await self.storage.get_user(callback.from_user.id)
        if user:
            user.language = lang
            await self.storage.save_user(user)
        await callback.message.edit_text("✅ 語言已設定為繁體中文" if lang == "zh-TW" else "✅ Language set to English")
        await callback.answer()

    # ------------------------------------------------------------------ demo & simulation

    async def cmd_demo(self, message: Message) -> None:
        user = await self.get_or_create_user(message)
        sim = self.simulations.get(user.telegram_id)
        if sim is not None and sim.is_running:
            await message.answer(
                "🎬 **Simulation running**\n\nYour price simulation is in progress.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⏹ Stop Simulation", callback_data="democtl:stop")],
                ]),
                parse_mode="Markdown",
            )
            return
        rows = [
            [InlineKeyboardButton(text="😊 Normal Market", callback_data="demo:normal")],
            [InlineKeyboardButton(text="📈 Volatile Market", callback_data="demo:volatile")],
            [InlineKeyboardButton(text="🚨 Critical Risk", callback_data="demo:critical")],
            [InlineKeyboardButton(text="✅ Safe Portfolio", callback_data="demo:safe")],
            [InlineKeyboardButton(text="🎬 Liquidation Simulation", callback_data="demo:liquidation_sim")],
            [InlineKeyboardButton(text="🎬 5 longs, one wallet (cross margin)", callback_data="demo:cross_longs_sim")],
        ]
        if user.is_demo_mode:
            rows.append([InlineKeyboardButton(text="❌ Disable Demo Mode", callback_data="demo:disable")])
            text = "🎮 **Demo Mode Active**\n\nSelect a scenario or start a simulation:"
        else:
            text = (
                "🎮 **Demo Mode**\n\nSelect a scenario:\n• Normal: typical market\n• Volatile: some risk\n"
                "• Critical: near liquidation\n• Safe: all positions safe\n"
                "• **Liquidation Sim**: watch prices walk toward liquidation with alerts (also on your /dashboard)\n"
                "• **5 longs, one wallet**: five correlated cross-margin longs fall together; see why the platform's "
                "liq lines lie, which position to handle first, and the account-level stress test"
            )
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="Markdown")

    async def callback_demo_scenario(self, callback: CallbackQuery) -> None:
        scenario = callback.data.split(":")[1]
        user_id = callback.from_user.id

        if scenario == "disable":
            await self.storage.set_demo_mode(user_id, False)
            self.positions.drop_demo(user_id)
            sim = self.simulations.get(user_id)
            if sim is not None:
                await sim.stop(source="telegram")
            await callback.message.edit_text(
                "📊 Demo mode **disabled**.\n\nUse /positions to view your live positions.", parse_mode="Markdown"
            )
            await callback.answer()
            return

        if scenario in ("liquidation_sim", "cross_longs_sim"):
            key = "cross_longs" if scenario == "cross_longs_sim" else "mixed"
            await self._start_liquidation_simulation(user_id, callback.message, key)
            await callback.answer("Starting simulation...")
            return

        if scenario not in SCENARIOS:
            await callback.answer()
            return
        await self.storage.set_demo_mode(user_id, True)
        self.positions.demo_adapter(user_id, scenario)
        await callback.message.edit_text(
            f"🎮 Demo mode **enabled** with **{scenario}** scenario.\n\n"
            "Use /positions to view simulated positions.\nUse /demo again to change scenario or start a simulation.",
            parse_mode="Markdown",
        )
        await callback.answer()

    async def _start_liquidation_simulation(self, user_id: int, message: Message, scenario: str = "mixed") -> None:
        await self.storage.set_demo_mode(user_id, True)
        user = await self.storage.get_user(user_id) or User(telegram_id=user_id, is_demo_mode=True)
        sim = self.simulation_for(user)
        sim.set_scenario(scenario)
        started = await sim.start(source="telegram")
        title = SIM_SCENARIOS[sim.scenario].title
        await message.edit_text(
            f"🎬 **Liquidation Simulation · {title}**\n\n"
            + ("Started. " if started else "Already running. ")
            + "This simulation is private to you: alerts arrive here and on your /dashboard, nowhere else.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏹ Stop Simulation", callback_data="democtl:stop")],
            ]),
            parse_mode="Markdown",
        )

    async def callback_demo_control(self, callback: CallbackQuery) -> None:
        if callback.data.split(":")[1] == "stop":
            sim = self.simulations.get(callback.from_user.id)
            if sim is not None:
                await sim.stop(source="telegram")
            await callback.message.edit_text(
                "⏹ Simulation stopped.\n\nUse /demo to start again or /positions to view current state."
            )
        await callback.answer()

    # ------------------------------------------------------------------ AI

    async def cmd_ask(self, message: Message) -> None:
        user = await self.get_or_create_user(message)
        question = (message.text or "").replace("/ask", "", 1).strip()
        if not question:
            await message.answer(
                "Usage: /ask <your question>\n\nExamples:\n• /ask What is my total risk?\n"
                "• /ask Should I add margin to my BTC position?\n• /ask 我的倉位風險如何？"
            )
            return
        positions, _ = await self._get_positions_for_user(user)
        if not positions:
            await message.answer("No positions to analyze. Connect an exchange or enable demo mode.")
            return
        status_msg = await message.answer("🤔 Analyzing...")
        news = None
        if any(s in question.lower() for s in ["news", "market", "sentiment", "新聞", "市場"]):
            news = get_news_stub(positions[0].symbol)
        answer = await self.llm.answer_portfolio_question(positions, question, user.language, news)
        await status_msg.edit_text(answer)

    async def handle_photo(self, message: Message, state: FSMContext) -> None:
        await self.get_or_create_user(message)
        status_msg = await message.answer("🔍 Analyzing screenshot...")
        try:
            file = await message.bot.get_file(message.photo[-1].file_id)
            image_bytes = (await message.bot.download_file(file.file_path)).read()
            parsed = await self.llm.parse_screenshot(image_bytes)
        except Exception as e:
            logger.error("Screenshot parsing failed for user %s: %s", message.from_user.id, type(e).__name__)
            await status_msg.edit_text(GENERIC_ERROR)
            return
        if not parsed:
            await status_msg.edit_text(
                "❌ Could not parse a position from that screenshot.\nMake sure it clearly shows a trading position."
            )
            return

        self._pending_screenshots[message.from_user.id] = parsed
        confidence = parsed.get("confidence", 0.5)
        emoji = "🟢" if confidence > 0.8 else "🟡" if confidence > 0.5 else "🔴"
        text = (
            f"📊 **Parsed Position** {emoji}\n\n"
            f"Symbol: **{parsed.get('symbol', 'Unknown')}**\nSide: {parsed.get('side', 'Unknown')}\n"
            f"Size: {parsed.get('size', 'Unknown')}\nEntry: ${parsed.get('entry_price', 0):,.2f}\n"
            f"Mark: ${parsed.get('mark_price', 0):,.2f}\nLiq: ${parsed.get('liquidation_price', 0):,.2f}\n"
            f"Leverage: {parsed.get('leverage', 'Unknown')}x\nVenue: {parsed.get('venue', 'Unknown')}\n"
            f"Confidence: {confidence:.0%}\n\n{'⚠️ Mock data (demo mode)' if parsed.get('mock') else ''}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm & Monitor", callback_data="screenshot:confirm"),
                InlineKeyboardButton(text="❌ Discard", callback_data="screenshot:discard"),
            ],
        ])
        await status_msg.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

    async def callback_screenshot_confirm(self, callback: CallbackQuery, state: FSMContext) -> None:
        action = callback.data.split(":")[1]
        user_id = callback.from_user.id
        if action == "discard":
            self._pending_screenshots.pop(user_id, None)
            await callback.message.edit_text("Screenshot discarded.")
            await callback.answer()
            return
        if action != "confirm":
            await callback.answer()
            return
        parsed = self._pending_screenshots.pop(user_id, None)
        if not parsed:
            await callback.answer("No pending screenshot")
            return
        await self.storage.set_demo_mode(user_id, True)
        self.positions.demo_adapter(user_id).add_custom_position(
            symbol=parsed.get("symbol", "BTCUSDT"),
            side=Side.LONG if parsed.get("side") == "long" else Side.SHORT,
            size=float(parsed.get("size", 0.1)),
            entry_price=float(parsed.get("entry_price", 65000)),
            mark_price=float(parsed.get("mark_price", 65000)),
            leverage=float(parsed.get("leverage", 10)),
            margin=float(parsed.get("margin", 650)),
            liquidation_price=float(parsed["liquidation_price"]) if parsed.get("liquidation_price") else None,
        )
        await callback.message.edit_text(
            f"✅ **Position added to your demo book**\n\nSymbol: {parsed.get('symbol')}\n"
            "Demo mode enabled. Use /positions to view.\n\nFor real monitoring, bind your exchange with /bind.",
            parse_mode="Markdown",
        )
        await callback.answer("Position added!")

    # ------------------------------------------------------------------ status & protective actions

    async def cmd_status(self, message: Message) -> None:
        user = await self.get_or_create_user(message)
        lines = []
        for b in user.bindings:
            mark = "✅" if b.is_active else "❌"
            if b.venue == "binance":
                lines.append(f"  {mark} Binance{' testnet' if b.is_testnet else ''}: key `{b.api_key_hint}`")
            elif b.venue == "hyperliquid" and b.wallet_address:
                lines.append(f"  {mark} Hyperliquid: `{b.wallet_address[:8]}...{b.wallet_address[-4:]}`")
        stats = self.actions_manager.get_user_stats(user.telegram_id)
        t = user.threshold
        await message.answer(
            f"**Account Status**\n\n🆔 User ID: `{user.telegram_id}`\n🌐 Language: {user.language}\n"
            f"🎮 Demo Mode: {'On' if user.is_demo_mode else 'Off'}\n\n"
            f"**Connected Exchanges:**\n{chr(10).join(lines) or '  None'}\n\n"
            f"**Alert Thresholds:**\n  ⚠️ Warning: {t.warning_distance_pct}%\n  🚨 Critical: {t.critical_distance_pct}%\n"
            f"  🆘 Emergency: {t.emergency_distance_pct}%\n\n"
            f"**Protective Actions (testnet):**\n  Enabled: {self.actions_manager.is_enabled()}\n"
            f"  Kill Switch: {'🔴 Active' if stats['kill_switch_active'] else '🟢 Off'}\n"
            f"  Today's total: ${stats['daily_total_usd']:.2f} / ${stats['daily_limit_usd']:.2f}\n",
            parse_mode="Markdown",
        )

    async def cmd_actions(self, message: Message) -> None:
        user = await self.get_or_create_user(message)
        pending = self.actions_manager.get_pending_actions(user.telegram_id)
        if not pending:
            await message.answer("No pending protective actions.\n\nActions are proposed when positions are at risk.")
            return
        response = "**Pending Actions:**\n\n"
        buttons = []
        for action in pending:
            adv = action.advice
            response += (
                f"🔸 {action.action_id}\n   {adv.action_type.value}: {adv.symbol}\n"
                f"   Amount: ${adv.margin_to_add_usd or 0:,.2f}\n   Status: {action.status.value}\n\n"
            )
            buttons.append([
                InlineKeyboardButton(text=f"✅ Confirm {action.action_id[-4:]}", callback_data=f"action:confirm:{action.action_id}"),
                InlineKeyboardButton(text="❌ Cancel", callback_data=f"action:cancel:{action.action_id}"),
            ])
        await message.answer(response, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    async def cmd_killswitch(self, message: Message) -> None:
        user = await self.get_or_create_user(message)
        if self.actions_manager.is_kill_switch_active(user.telegram_id):
            self.actions_manager.deactivate_kill_switch(user.telegram_id)
            await message.answer("🟢 **Kill Switch Deactivated**\n\nProtective actions can be proposed again.", parse_mode="Markdown")
        else:
            self.actions_manager.activate_kill_switch(user.telegram_id)
            await message.answer(
                "🔴 **Kill Switch Activated**\n\nAll pending actions cancelled. No new actions will execute.\n"
                "Use /killswitch again to deactivate.",
                parse_mode="Markdown",
            )

    async def callback_action_confirm(self, callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        kind = parts[1]
        user_id = callback.from_user.id

        if kind == "dismiss":
            await self._delete_quietly(callback.message)
            await callback.answer("Dismissed")
            return

        if kind == "propose":
            symbol = parts[2] if len(parts) > 2 else ""
            user = await self.storage.get_user(user_id)
            if not user:
                await callback.answer("User not found")
                return
            snapshot = await self._snapshot(user)
            position = next((p for p in snapshot.positions if p.symbol == symbol), None)
            if not position:
                await callback.answer("Position not found")
                return
            ctx = snapshot.context
            if ctx.computable(position):
                simulator = RescueSimulator(ctx.fees, user.threshold.safety_target_pct)
                advice = simulator.simulate(position, ctx.account_for(position), ctx.funding_for(position), ctx.now).margin_advice()
            else:
                advice = AdviceCalculator.calculate_margin_to_add(position, user.threshold.safety_target_pct)
            action, msg = self.actions_manager.propose_action(user_id, advice, position)
            if action:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Confirm", callback_data=f"action:confirm:{action.action_id}"),
                    InlineKeyboardButton(text="❌ Cancel", callback_data=f"action:cancel:{action.action_id}"),
                ]])
                await callback.message.answer(
                    f"**Action Proposed**\n\nAdd **${advice.margin_to_add_usd:,.2f}** margin to {symbol}\n"
                    f"→ Distance improves to {advice.target_distance_pct:.0f}%\n\n"
                    "⚠️ Executes only for testnet positions. Tap confirm to execute.",
                    reply_markup=keyboard,
                    parse_mode="Markdown",
                )
            else:
                await callback.message.answer(f"❌ {msg}")
            await callback.answer()
            return

        action_id = parts[2] if len(parts) > 2 else ""
        if kind == "confirm":
            ok, msg = self.actions_manager.confirm_action(action_id, user_id)
            if ok:
                ok, msg = await self.actions_manager.execute_action(action_id, user_id)
                await callback.message.edit_text(f"✅ Action completed: {msg}" if ok else f"⚠️ Execution: {msg}")
            else:
                await callback.message.edit_text(f"❌ {msg}")
        elif kind == "cancel":
            ok, msg = self.actions_manager.cancel_action(action_id, user_id)
            await callback.message.edit_text(f"{'✅' if ok else '❌'} {msg}")
        await callback.answer()

    # ------------------------------------------------------------------ dashboard login

    async def cmd_dashboard(self, message: Message, command: Optional[CommandObject] = None) -> None:
        if message.chat.type != ChatType.PRIVATE:
            await message.answer("🔒 Dashboard links are personal. Send /dashboard to me in a private chat.")
            return
        user = await self.get_or_create_user(message)
        arg = ((command.args if command else None) or "").strip().lower()
        if arg == "revoke":
            await self.storage.bump_session_epoch(user.telegram_id)
            await message.answer("🔒 Signed out of the dashboard everywhere. Unused login links no longer work.")
            return
        if not self.ctx.settings.dashboard_login_available:
            await message.answer(
                "🖥 The web dashboard is not enabled on this LiqGuard instance. "
                "Everything is available here in Telegram: /positions, /risk, /actions."
            )
            return

        token = await self.ctx.login_links.create(user.telegram_id)
        url = self.ctx.dashboard_login_url(token)
        minutes = max(1, self.ctx.settings.login_link_ttl // 60)
        keyboard = None
        if url.startswith("https://"):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Open my dashboard", url=url)]])
        await message.answer(
            "🖥 Your private LiqGuard dashboard\n\n"
            f"{url}\n\n"
            f"• Works once and expires in {minutes} min\n"
            "• Shows only your positions, alerts and audit log\n"
            "• Don't share it — anyone with the link can sign in as you until it's used\n"
            "• /dashboard revoke signs out every device",
            reply_markup=keyboard,
            parse_mode=None,
            disable_web_page_preview=True,
        )
