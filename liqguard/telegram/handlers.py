"""Telegram bot handlers for LiqGuard."""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from liqguard.connectors import BinanceConnector, DemoConnector, HyperliquidConnector
from liqguard.llm import MetaLLMClient, get_news_stub
from liqguard.models import ExchangeBinding, Position, Side, User, Venue
from liqguard.services import AlertManager, EncryptedStorage, RiskEngine
from liqguard.services.advice import ActionableAdvice, AdviceCalculator
from liqguard.services.portfolio import PortfolioAnalyzer, format_portfolio_overview_text
from liqguard.services.protective_actions import (
    ActionStatus,
    ExecutionTier,
    ProtectiveActionConfig,
    ProtectiveActionsManager,
)
from liqguard.services.simulation import MarketSimulation
from liqguard.telegram.sim_bridge import TelegramSimulationBridge

logger = logging.getLogger(__name__)

router = Router()


class BindingStates(StatesGroup):
    """FSM states for binding flow."""
    waiting_for_venue = State()
    waiting_for_binance_api_key = State()
    waiting_for_binance_api_secret = State()
    waiting_for_hyperliquid_address = State()


class ThresholdStates(StatesGroup):
    """FSM states for threshold configuration."""
    waiting_for_threshold_type = State()
    waiting_for_value = State()


class ScreenshotStates(StatesGroup):
    """FSM states for screenshot import flow."""
    waiting_for_confirmation = State()


class BotHandlers:
    """Handler class for the Telegram bot."""

    def __init__(
        self,
        storage: EncryptedStorage,
        risk_engine: RiskEngine,
        alert_manager: AlertManager,
        llm_client: MetaLLMClient,
        actions_manager: Optional[ProtectiveActionsManager] = None,
        simulation: Optional[MarketSimulation] = None,
        sim_bridge: Optional[TelegramSimulationBridge] = None,
    ):
        self.storage = storage
        self.risk_engine = risk_engine
        self.alert_manager = alert_manager
        self.llm = llm_client
        self.actions_manager = actions_manager or ProtectiveActionsManager()
        self.simulation = simulation
        self.sim_bridge = sim_bridge

        self._demo_connectors: dict[int, DemoConnector] = {}
        self._temp_binance_key: dict[int, str] = {}
        self._pending_screenshots: dict[int, dict] = {}
        self._demo_simulations: dict[int, asyncio.Task] = {}
        self._bot: Optional[Bot] = None

    def get_demo_connector(self, user_id: int, scenario: str = "normal") -> DemoConnector:
        """Get or create demo connector for a user."""
        if user_id not in self._demo_connectors:
            self._demo_connectors[user_id] = DemoConnector(scenario)
        return self._demo_connectors[user_id]

    async def get_or_create_user(self, message: Message) -> User:
        """Get or create user from message."""
        user = await self.storage.get_user(message.from_user.id)
        if not user:
            user = User(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
            )
            await self.storage.save_user(user)
        return user

    def register(self, dp: Dispatcher, bot: Bot) -> None:
        """Register all handlers with the dispatcher."""
        self._bot = bot
        dp.include_router(router)

        router.message.register(self.cmd_start, CommandStart())
        router.message.register(self.cmd_help, Command("help"))
        router.message.register(self.cmd_bind, Command("bind"))
        router.message.register(self.cmd_unbind, Command("unbind"))
        router.message.register(self.cmd_positions, Command("positions"))
        router.message.register(self.cmd_threshold, Command("threshold"))
        router.message.register(self.cmd_demo, Command("demo"))
        router.message.register(self.cmd_ask, Command("ask"))
        router.message.register(self.cmd_language, Command("language", "lang"))
        router.message.register(self.cmd_status, Command("status"))
        router.message.register(self.cmd_risk, Command("risk"))
        router.message.register(self.cmd_actions, Command("actions"))
        router.message.register(self.cmd_killswitch, Command("killswitch"))

        router.callback_query.register(self.callback_venue_selected, F.data.startswith("venue:"))
        router.callback_query.register(self.callback_unbind_venue, F.data.startswith("unbind:"))
        router.callback_query.register(self.callback_demo_scenario, F.data.startswith("demo:"))
        router.callback_query.register(self.callback_threshold_type, F.data.startswith("threshold:"))
        router.callback_query.register(self.callback_language, F.data.startswith("lang:"))
        router.callback_query.register(self.callback_screenshot_confirm, F.data.startswith("screenshot:"))
        router.callback_query.register(self.callback_action_confirm, F.data.startswith("action:"))
        router.callback_query.register(self.callback_demo_control, F.data.startswith("democtl:"))
        router.callback_query.register(self.callback_risk, F.data.startswith("risk:"))

        router.message.register(self.process_binance_api_key, BindingStates.waiting_for_binance_api_key)
        router.message.register(self.process_binance_api_secret, BindingStates.waiting_for_binance_api_secret)
        router.message.register(self.process_hyperliquid_address, BindingStates.waiting_for_hyperliquid_address)
        router.message.register(self.process_threshold_value, ThresholdStates.waiting_for_value)

        router.message.register(self.handle_photo, F.photo)

    async def cmd_start(self, message: Message) -> None:
        """Handle /start command."""
        await self.get_or_create_user(message)

        welcome = """
🛡️ **Welcome to LiqGuard!**

I help you monitor your perpetual futures positions and warn you before liquidation.

**Supported Exchanges:**
• Binance USDⓈ-M Futures (API key required)
• Hyperliquid (wallet address only)

**Quick Start:**
1. Use /bind to connect your exchange
2. Use /positions to view your positions
3. Use /threshold to set alert levels
4. Use /demo to try with simulated positions

**Commands:**
/help - Show all commands
/bind - Connect an exchange
/positions - View positions & risk
/demo - Toggle demo mode

Type /help for more options.
"""
        await message.answer(welcome, parse_mode="Markdown")

    async def cmd_help(self, message: Message) -> None:
        """Handle /help command."""
        user = await self.get_or_create_user(message)

        if user.language == "zh-TW":
            help_text = """
🛡️ **LiqGuard 指令**

**連接交易所**
/bind - 連接交易所（Binance 或 Hyperliquid）
/unbind - 移除交易所連接

**監控倉位**
/positions - 查看所有倉位和風險評估
/status - 查看帳戶狀態

**設定**
/threshold - 設定警報閾值
/language - 切換語言
/demo - 演示模式（含價格模擬）

**AI 功能**
/ask <問題> - 詢問關於您的倉位
📸 發送截圖 - 解析倉位截圖

**保護操作（測試網）**
/actions - 查看待處理操作
/killswitch - 緊急停止開關

**安全說明**
• 僅使用只讀 API 金鑰
• 金鑰以加密方式存儲
• 執行操作僅限測試網
"""
        else:
            help_text = """
🛡️ **LiqGuard Commands**

**Connect Exchanges**
/bind - Connect an exchange (Binance or Hyperliquid)
/unbind - Remove exchange connection

**Monitor Positions**
/positions - View all positions with risk assessment
/status - View account status

**Settings**
/threshold - Configure alert thresholds
/language - Change language
/demo - Demo mode (with price simulation)

**AI Features**
/ask <question> - Ask about your positions
📸 Send a screenshot - Parse position from image

**Protective Actions (Testnet)**
/actions - View pending actions
/killswitch - Emergency stop

**Security Notes**
• Only use READ-ONLY API keys
• Keys are stored encrypted
• Execution only on testnet
"""
        await message.answer(help_text, parse_mode="Markdown")

    async def cmd_bind(self, message: Message, state: FSMContext) -> None:
        """Handle /bind command."""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟡 Binance Futures", callback_data="venue:binance")],
            [InlineKeyboardButton(text="🟢 Hyperliquid", callback_data="venue:hyperliquid")],
        ])

        await message.answer(
            "Select the exchange to connect:\n\n"
            "• **Binance**: Requires API key (read-only)\n"
            "• **Hyperliquid**: Only wallet address needed",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    async def callback_venue_selected(self, callback: CallbackQuery, state: FSMContext) -> None:
        """Handle venue selection callback."""
        venue = callback.data.split(":")[1]

        if venue == "binance":
            await state.set_state(BindingStates.waiting_for_binance_api_key)
            await callback.message.edit_text(
                "🔑 **Binance API Key Setup**\n\n"
                "Please send your Binance API key.\n\n"
                "⚠️ **IMPORTANT**: Use a READ-ONLY key!\n"
                "• Enable: Read Info only\n"
                "• Disable: Trading, Withdrawals, IP restriction recommended\n\n"
                "Send your API key now:",
                parse_mode="Markdown"
            )
        elif venue == "hyperliquid":
            await state.set_state(BindingStates.waiting_for_hyperliquid_address)
            await callback.message.edit_text(
                "🔗 **Hyperliquid Wallet Setup**\n\n"
                "Please send your wallet address (0x...).\n\n"
                "✅ No API key needed - we only read public data.\n\n"
                "Send your wallet address now:",
                parse_mode="Markdown"
            )

        await callback.answer()

    async def process_binance_api_key(self, message: Message, state: FSMContext) -> None:
        """Process Binance API key input."""
        api_key = message.text.strip()

        if len(api_key) < 20:
            await message.answer("❌ Invalid API key format. Please try again.")
            return

        self._temp_binance_key[message.from_user.id] = api_key
        await state.set_state(BindingStates.waiting_for_binance_api_secret)
        await message.delete()
        await message.answer(
            "✅ API key received (hidden for security).\n\nNow send your API Secret:",
            parse_mode="Markdown"
        )

    async def process_binance_api_secret(self, message: Message, state: FSMContext) -> None:
        """Process Binance API secret and validate."""
        api_secret = message.text.strip()
        api_key = self._temp_binance_key.pop(message.from_user.id, None)

        if not api_key:
            await message.answer("❌ Session expired. Please start over with /bind")
            await state.clear()
            return

        await message.delete()
        status_msg = await message.answer("🔄 Validating API credentials...")

        connector = BinanceConnector(api_key, api_secret)
        try:
            is_valid, msg, permissions = await connector.validate_credentials()

            if not is_valid:
                await status_msg.edit_text(f"❌ Validation failed: {msg}")
                await state.clear()
                return

            if "WITHDRAW" in permissions:
                await status_msg.edit_text(
                    "🚫 **REJECTED**: API key has WITHDRAW permission!\n\n"
                    "This is a security risk. Please create a new key with:\n"
                    "• ✅ Enable Reading\n• ❌ Disable Trading\n• ❌ Disable Withdrawals\n\n"
                    "Use /bind to try again.",
                    parse_mode="Markdown"
                )
                await state.clear()
                return

            warning = ""
            if "FUTURES_TRADE" in permissions:
                warning = "\n\n⚠️ Warning: Key has TRADE permission. Recommend using read-only key."

            user = await self.get_or_create_user(message)
            binding = ExchangeBinding(
                venue="binance",
                binding_type="api_key",
                api_key=api_key,
                api_secret_encrypted=api_secret,
                permissions=permissions,
            )
            await self.storage.save_binding(user.telegram_id, binding)

            await status_msg.edit_text(
                f"✅ **Binance connected successfully!**{warning}\n\n"
                f"Detected permissions: {', '.join(permissions)}\n\n"
                "Use /positions to view your positions.",
                parse_mode="Markdown"
            )

        except Exception as e:
            logger.error(f"Error validating Binance credentials: {e}")
            await status_msg.edit_text(f"❌ Error: {str(e)}")
        finally:
            await connector.close()
            await state.clear()

    async def process_hyperliquid_address(self, message: Message, state: FSMContext) -> None:
        """Process Hyperliquid wallet address."""
        address = message.text.strip().lower()

        if not re.match(r"^0x[a-f0-9]{40}$", address):
            await message.answer(
                "❌ Invalid wallet address format.\nMust be a 42-character hex address starting with 0x.\n\nPlease try again:"
            )
            return

        status_msg = await message.answer("🔄 Validating wallet address...")

        connector = HyperliquidConnector(address)
        try:
            is_valid, msg, _ = await connector.validate_credentials()

            if not is_valid:
                await status_msg.edit_text(f"❌ Validation failed: {msg}")
                await state.clear()
                return

            user = await self.get_or_create_user(message)
            binding = ExchangeBinding(
                venue="hyperliquid",
                binding_type="wallet",
                wallet_address=address,
                permissions=["read_positions"],
            )
            await self.storage.save_binding(user.telegram_id, binding)

            await status_msg.edit_text(
                f"✅ **Hyperliquid wallet connected!**\n\nAddress: `{address[:8]}...{address[-6:]}`\n\n"
                "Use /positions to view your positions.",
                parse_mode="Markdown"
            )

        except Exception as e:
            logger.error(f"Error validating Hyperliquid address: {e}")
            await status_msg.edit_text(f"❌ Error: {str(e)}")
        finally:
            await connector.close()
            await state.clear()

    async def cmd_unbind(self, message: Message) -> None:
        """Handle /unbind command."""
        user = await self.get_or_create_user(message)

        if not user.bindings:
            await message.answer("You have no connected exchanges.")
            return

        buttons = []
        for binding in user.bindings:
            if binding.is_active:
                label = f"{'🟡' if binding.venue == 'binance' else '🟢'} {binding.venue.title()}"
                buttons.append([InlineKeyboardButton(text=label, callback_data=f"unbind:{binding.venue}")])

        if not buttons:
            await message.answer("You have no active connections.")
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer("Select exchange to disconnect:", reply_markup=keyboard)

    async def callback_unbind_venue(self, callback: CallbackQuery) -> None:
        """Handle unbind venue callback."""
        venue = callback.data.split(":")[1]
        user_id = callback.from_user.id

        success = await self.storage.delete_binding(user_id, venue)

        if success:
            await callback.message.edit_text(f"✅ {venue.title()} disconnected.")
        else:
            await callback.message.edit_text(f"❌ Failed to disconnect {venue}.")

        await callback.answer()

    async def _get_positions_for_user(self, user: User) -> tuple[list[Position], str]:
        """Get positions for a user (demo or live)."""
        if user.is_demo_mode:
            if self._uses_shared_simulation(user.telegram_id) and self.simulation.state != "idle":
                return list(self.simulation.positions), "📊 Demo Mode · market simulation"
            connector = self.get_demo_connector(user.telegram_id)
            positions = await connector.get_positions()
            return positions, "📊 Demo Mode"

        if not user.has_any_binding():
            return [], ""

        positions = []
        for binding in user.bindings:
            if not binding.is_active:
                continue

            try:
                if binding.venue == "binance":
                    connector = BinanceConnector(binding.api_key, binding.api_secret_encrypted)
                elif binding.venue == "hyperliquid":
                    connector = HyperliquidConnector(binding.wallet_address)
                else:
                    continue

                venue_positions = await connector.get_positions()
                positions.extend(venue_positions)
                await connector.close()

            except Exception as e:
                logger.error(f"Error fetching from {binding.venue}: {e}")

        return positions, "📊 Live Positions"

    async def cmd_positions(self, message: Message) -> None:
        """Handle /positions command with actionable advice."""
        user = await self.get_or_create_user(message)

        positions, venue_label = await self._get_positions_for_user(user)

        if not venue_label:
            await message.answer(
                "No exchanges connected.\n\nUse /bind to connect an exchange or /demo to try demo mode."
            )
            return

        if not positions:
            await message.answer(f"{venue_label}\n\nNo open positions found.")
            return

        assessments = self.risk_engine.assess_positions(positions)
        response = f"**{venue_label}**\n\n"

        for assessment in assessments:
            pos = assessment.position

            risk_emoji = {"SAFE": "✅", "WARNING": "⚠️", "CRITICAL": "🚨", "EMERGENCY": "🆘"}.get(
                assessment.risk_level_str, "❓"
            )
            side_emoji = "📈" if pos.side.value == "long" else "📉"

            response += (
                f"{risk_emoji} **{pos.symbol}** {side_emoji} {pos.side.value.upper()}\n"
                f"├ Size: {pos.size}\n"
                f"├ Entry: ${pos.entry_price:,.2f}\n"
                f"├ Mark: ${pos.mark_price:,.2f}\n"
                f"├ Liq: ${pos.liquidation_price:,.2f}\n"
                f"├ Distance: **{assessment.distance_to_liquidation_pct:.1f}%**\n"
                f"├ PnL: ${pos.unrealized_pnl:,.2f}\n"
                f"├ Leverage: {pos.leverage}x\n"
            )

            if assessment.margin_to_safety > 0:
                advice = AdviceCalculator.calculate_margin_to_add(pos)
                advice_text = AdviceCalculator.format_advice_text(advice, user.language)
                response += f"└ {advice_text}\n"
            else:
                response += "└ Position is safe\n"

            response += "\n"

        if len(response) > 4000:
            response = response[:3900] + "\n\n... (truncated)"

        buttons = []
        if self.actions_manager.is_enabled():
            for assessment in assessments[:3]:
                if assessment.margin_to_safety > 0:
                    buttons.append([
                        InlineKeyboardButton(
                            text=f"💰 Add margin to {assessment.position.symbol}",
                            callback_data=f"action:propose:{assessment.position.symbol}"
                        )
                    ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        await message.answer(response, parse_mode="Markdown", reply_markup=keyboard)

    async def cmd_threshold(self, message: Message, state: FSMContext) -> None:
        """Handle /threshold command."""
        user = await self.get_or_create_user(message)
        t = user.threshold

        current = (
            f"**Current Thresholds:**\n"
            f"• Warning: {t.warning_distance_pct}%\n"
            f"• Critical: {t.critical_distance_pct}%\n"
            f"• Emergency: {t.emergency_distance_pct}%\n"
            f"• Safety target: {t.safety_target_pct}%\n"
            f"• Cooldown: {t.cooldown_seconds}s\n\n"
            "Select a threshold to modify:"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⚠️ Warning ({t.warning_distance_pct}%)", callback_data="threshold:warning")],
            [InlineKeyboardButton(text=f"🚨 Critical ({t.critical_distance_pct}%)", callback_data="threshold:critical")],
            [InlineKeyboardButton(text=f"🆘 Emergency ({t.emergency_distance_pct}%)", callback_data="threshold:emergency")],
            [InlineKeyboardButton(text=f"🎯 Safety Target ({t.safety_target_pct}%)", callback_data="threshold:safety")],
        ])

        await message.answer(current, reply_markup=keyboard, parse_mode="Markdown")

    async def callback_threshold_type(self, callback: CallbackQuery, state: FSMContext) -> None:
        """Handle threshold type selection."""
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
            parse_mode="Markdown"
        )
        await callback.answer()

    async def process_threshold_value(self, message: Message, state: FSMContext) -> None:
        """Process threshold value input."""
        try:
            value = float(message.text.strip().replace("%", ""))
            if value <= 0 or value > 100:
                raise ValueError("Must be between 0 and 100")
        except ValueError as e:
            await message.answer(f"❌ Invalid value: {e}\nPlease enter a number between 0 and 100.")
            return

        data = await state.get_data()
        threshold_type = data.get("threshold_type")

        user = await self.get_or_create_user(message)
        t = user.threshold

        if threshold_type == "warning":
            t.warning_distance_pct = value
        elif threshold_type == "critical":
            t.critical_distance_pct = value
        elif threshold_type == "emergency":
            t.emergency_distance_pct = value
        elif threshold_type == "safety":
            t.safety_target_pct = value

        await self.storage.update_threshold(user.telegram_id, t)
        await state.clear()

        await message.answer(
            f"✅ {threshold_type.title()} threshold updated to {value}%\n\nUse /threshold to view all settings."
        )

    async def cmd_demo(self, message: Message) -> None:
        """Handle /demo command with simulation options."""
        user = await self.get_or_create_user(message)
        user_id = message.from_user.id

        if user_id in self._demo_simulations:
            task = self._demo_simulations[user_id]
            if not task.done():
                await message.answer(
                    "🎬 **Simulation Running**\n\nA price simulation is in progress.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⏹ Stop Simulation", callback_data="democtl:stop")],
                    ]),
                    parse_mode="Markdown"
                )
                return

        if user.is_demo_mode:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="😊 Normal Market", callback_data="demo:normal")],
                [InlineKeyboardButton(text="📈 Volatile Market", callback_data="demo:volatile")],
                [InlineKeyboardButton(text="🚨 Critical Risk", callback_data="demo:critical")],
                [InlineKeyboardButton(text="✅ Safe Portfolio", callback_data="demo:safe")],
                [InlineKeyboardButton(text="🎬 Liquidation Simulation", callback_data="demo:liquidation_sim")],
                [InlineKeyboardButton(text="❌ Disable Demo Mode", callback_data="demo:disable")],
            ])

            await message.answer(
                "🎮 **Demo Mode Active**\n\nSelect a scenario or start simulation:",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        else:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="😊 Normal Market", callback_data="demo:normal")],
                [InlineKeyboardButton(text="📈 Volatile Market", callback_data="demo:volatile")],
                [InlineKeyboardButton(text="🚨 Critical Risk", callback_data="demo:critical")],
                [InlineKeyboardButton(text="✅ Safe Portfolio", callback_data="demo:safe")],
                [InlineKeyboardButton(text="🎬 Liquidation Simulation", callback_data="demo:liquidation_sim")],
            ])

            await message.answer(
                "🎮 **Demo Mode**\n\nSelect a scenario to simulate:\n"
                "• Normal: Typical market conditions\n"
                "• Volatile: High volatility, some risk\n"
                "• Critical: Positions near liquidation\n"
                "• Safe: All positions are safe\n"
                "• **Liquidation Sim**: Watch prices move toward liquidation with alerts",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )

    async def callback_demo_scenario(self, callback: CallbackQuery) -> None:
        """Handle demo scenario selection."""
        scenario = callback.data.split(":")[1]
        user_id = callback.from_user.id

        if scenario == "disable":
            await self.storage.set_demo_mode(user_id, False)
            if user_id in self._demo_connectors:
                del self._demo_connectors[user_id]
            await callback.message.edit_text(
                "📊 Demo mode **disabled**.\n\nUse /positions to view your live positions.",
                parse_mode="Markdown"
            )
            await callback.answer()
            return

        if scenario == "liquidation_sim":
            await self._start_liquidation_simulation(user_id, callback.message)
            await callback.answer("Starting simulation...")
            return

        await self.storage.set_demo_mode(user_id, True)
        self._demo_connectors[user_id] = DemoConnector(scenario)
        if self.sim_bridge is not None:
            self.sim_bridge.add_chat(user_id)

        await callback.message.edit_text(
            f"🎮 Demo mode **enabled** with **{scenario}** scenario.\n\n"
            "Use /positions to view simulated positions.\nUse /demo again to change scenario or start simulation.",
            parse_mode="Markdown"
        )
        await callback.answer()

    def _uses_shared_simulation(self, user_id: int) -> bool:
        return self.simulation is not None and self.sim_bridge is not None and self.sim_bridge.has_chat(user_id)

    async def _start_liquidation_simulation(self, user_id: int, message: Message) -> None:
        """Start a liquidation price simulation."""
        await self.storage.set_demo_mode(user_id, True)

        if self.simulation is not None and self.sim_bridge is not None:
            self.sim_bridge.add_chat(user_id)
            started = await self.simulation.start(source="telegram")
            await message.edit_text(
                "🎬 **Liquidation Simulation**\n\n"
                + ("Started. " if started else "Already running — you're subscribed. ")
                + "The same price path drives the web dashboard, so alerts appear in both places.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⏹ Stop Simulation", callback_data="democtl:stop")],
                ]),
                parse_mode="Markdown"
            )
            return

        connector = DemoConnector("liquidation_path")
        self._demo_connectors[user_id] = connector

        await message.edit_text(
            "🎬 **Liquidation Simulation Starting**\n\n"
            "Prices will gradually move toward liquidation.\n"
            "Alerts will fire at:\n"
            "⚠️ Warning (20%)\n🚨 Critical (10%)\n🆘 Emergency (5%)\n\n"
            "Watch for alerts...",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏹ Stop Simulation", callback_data="democtl:stop")],
            ]),
            parse_mode="Markdown"
        )

        async def alert_callback(level: str, pos: Position, distance: float):
            emoji = {"warning": "⚠️", "critical": "🚨", "emergency": "🆘"}.get(level, "❓")
            advice = AdviceCalculator.calculate_margin_to_add(pos)

            alert_msg = (
                f"{emoji} **{level.upper()} ALERT**\n\n"
                f"**{pos.symbol}** {pos.side.value.upper()}\n"
                f"Distance: **{distance:.1f}%**\n"
                f"Mark: ${pos.mark_price:,.2f}\n"
                f"Liq: ${pos.liquidation_price:,.2f}\n\n"
            )

            if advice.margin_to_add_usd and advice.margin_to_add_usd > 0:
                alert_msg += f"💰 Add **${advice.margin_to_add_usd:,.2f}** margin for safety"

                buttons = [[
                    InlineKeyboardButton(
                        text=f"💰 Add ${advice.margin_to_add_usd:.0f} margin",
                        callback_data=f"action:propose:{pos.symbol}"
                    ),
                    InlineKeyboardButton(text="❌ Dismiss", callback_data="action:dismiss"),
                ]]
                keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            else:
                keyboard = None

            if self._bot:
                await self._bot.send_message(user_id, alert_msg, parse_mode="Markdown", reply_markup=keyboard)

        connector.set_alert_callback(alert_callback)

        async def run_simulation():
            try:
                await connector.start_liquidation_simulation(interval_seconds=3.0)
            except asyncio.CancelledError:
                pass
            finally:
                if user_id in self._demo_simulations:
                    del self._demo_simulations[user_id]

        task = asyncio.create_task(run_simulation())
        self._demo_simulations[user_id] = task

    async def callback_demo_control(self, callback: CallbackQuery) -> None:
        """Handle demo control callbacks."""
        action = callback.data.split(":")[1]
        user_id = callback.from_user.id

        if action == "stop":
            if self._uses_shared_simulation(user_id):
                await self.simulation.stop(source="telegram")
            if user_id in self._demo_simulations:
                self._demo_simulations[user_id].cancel()
                del self._demo_simulations[user_id]
            if user_id in self._demo_connectors:
                self._demo_connectors[user_id].stop_simulation()

            await callback.message.edit_text(
                "⏹ Simulation stopped.\n\nUse /demo to start again or /positions to view current state.",
                parse_mode="Markdown"
            )

        await callback.answer()

    async def cmd_ask(self, message: Message) -> None:
        """Handle /ask command with position context and optional news."""
        user = await self.get_or_create_user(message)

        question = message.text.replace("/ask", "").strip()
        if not question:
            await message.answer(
                "Usage: /ask <your question>\n\n"
                "Examples:\n"
                "• /ask What is my total risk?\n"
                "• /ask Should I add margin to my BTC position?\n"
                "• /ask 我的倉位風險如何？"
            )
            return

        positions, _ = await self._get_positions_for_user(user)

        if not positions:
            await message.answer("No positions to analyze. Connect an exchange or enable demo mode.")
            return

        status_msg = await message.answer("🤔 Analyzing...")

        news_context = None
        if any(s in question.lower() for s in ["news", "market", "sentiment", "新聞", "市場"]):
            if positions:
                news_context = get_news_stub(positions[0].symbol)

        answer = await self.llm.answer_portfolio_question(
            positions, question, user.language, news_context
        )

        await status_msg.edit_text(answer)

    async def cmd_language(self, message: Message) -> None:
        """Handle /language command."""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🇺🇸 English", callback_data="lang:en")],
            [InlineKeyboardButton(text="🇹🇼 繁體中文", callback_data="lang:zh-TW")],
        ])

        await message.answer("Select your preferred language:", reply_markup=keyboard)

    async def callback_language(self, callback: CallbackQuery) -> None:
        """Handle language selection."""
        lang = callback.data.split(":")[1]
        user_id = callback.from_user.id

        user = await self.storage.get_user(user_id)
        if user:
            user.language = lang
            await self.storage.save_user(user)

        if lang == "zh-TW":
            await callback.message.edit_text("✅ 語言已設定為繁體中文")
        else:
            await callback.message.edit_text("✅ Language set to English")

        await callback.answer()

    async def cmd_status(self, message: Message) -> None:
        """Handle /status command."""
        user = await self.get_or_create_user(message)

        bindings_info = ""
        if user.bindings:
            for b in user.bindings:
                status = "✅" if b.is_active else "❌"
                if b.venue == "binance":
                    bindings_info += f"  {status} Binance: `{b.api_key[:8]}...`\n"
                elif b.venue == "hyperliquid":
                    addr = b.wallet_address
                    bindings_info += f"  {status} Hyperliquid: `{addr[:8]}...{addr[-4:]}`\n"
        else:
            bindings_info = "  None\n"

        action_stats = self.actions_manager.get_user_stats(user.telegram_id)

        status_text = (
            f"**Account Status**\n\n"
            f"🆔 User ID: `{user.telegram_id}`\n"
            f"🌐 Language: {user.language}\n"
            f"🎮 Demo Mode: {'On' if user.is_demo_mode else 'Off'}\n\n"
            f"**Connected Exchanges:**\n{bindings_info}\n"
            f"**Alert Thresholds:**\n"
            f"  ⚠️ Warning: {user.threshold.warning_distance_pct}%\n"
            f"  🚨 Critical: {user.threshold.critical_distance_pct}%\n"
            f"  🆘 Emergency: {user.threshold.emergency_distance_pct}%\n\n"
            f"**Protective Actions:**\n"
            f"  Enabled: {self.actions_manager.is_enabled()}\n"
            f"  Kill Switch: {'🔴 Active' if action_stats['kill_switch_active'] else '🟢 Off'}\n"
            f"  Today's total: ${action_stats['daily_total_usd']:.2f} / ${action_stats['daily_limit_usd']:.2f}\n"
        )

        await message.answer(status_text, parse_mode="Markdown")

    async def _risk_overview_text(self, overview, language: str) -> str:
        summary = await self.llm.summarize_portfolio(overview, language)
        tag = "Muse (mock)" if summary.get("mock") else "Muse"
        return f"🧠 _{tag}:_ {summary['headline']}\n\n" + format_portfolio_overview_text(overview, language)

    async def cmd_risk(self, message: Message) -> None:
        """Handle /risk command - portfolio risk overview."""
        user = await self.get_or_create_user(message)
        
        positions, venue_label = await self._get_positions_for_user(user)
        
        if not venue_label:
            await message.answer(
                "No exchanges connected.\n\nUse /bind to connect an exchange or /demo to try demo mode."
            )
            return
        
        if not positions:
            await message.answer(f"{venue_label}\n\nNo open positions found.")
            return
        
        analyzer = PortfolioAnalyzer(user.threshold)
        overview = analyzer.analyze_portfolio(positions)
        
        text = await self._risk_overview_text(overview, user.language)
        
        buttons = []
        if overview.riskiest_position and overview.riskiest_position.margin_advice:
            adv = overview.riskiest_position.margin_advice
            buttons.append([
                InlineKeyboardButton(
                    text=f"💰 Add ${adv.margin_to_add_usd:.0f} to {overview.riskiest_position.symbol}",
                    callback_data=f"action:propose:{overview.riskiest_position.symbol}"
                )
            ])
        
        buttons.append([
            InlineKeyboardButton(text="📋 Detailed Positions", callback_data="risk:positions"),
            InlineKeyboardButton(text="🔄 Refresh", callback_data="risk:refresh"),
        ])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)

    async def cmd_actions(self, message: Message) -> None:
        """Handle /actions command - show pending protective actions."""
        user = await self.get_or_create_user(message)

        pending = self.actions_manager.get_pending_actions(user.telegram_id)

        if not pending:
            await message.answer(
                "No pending protective actions.\n\n"
                "Actions are proposed when positions are at risk."
            )
            return

        response = "**Pending Actions:**\n\n"
        buttons = []

        for action in pending:
            adv = action.advice
            response += (
                f"🔸 {action.action_id}\n"
                f"   {adv.action_type.value}: {adv.symbol}\n"
                f"   Amount: ${adv.margin_to_add_usd or 0:,.2f}\n"
                f"   Status: {action.status.value}\n\n"
            )
            buttons.append([
                InlineKeyboardButton(text=f"✅ Confirm {action.action_id[-4:]}", callback_data=f"action:confirm:{action.action_id}"),
                InlineKeyboardButton(text=f"❌ Cancel", callback_data=f"action:cancel:{action.action_id}"),
            ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        await message.answer(response, parse_mode="Markdown", reply_markup=keyboard)

    async def cmd_killswitch(self, message: Message) -> None:
        """Handle /killswitch command - emergency stop."""
        user = await self.get_or_create_user(message)

        if self.actions_manager.is_kill_switch_active(user.telegram_id):
            self.actions_manager.deactivate_kill_switch(user.telegram_id)
            await message.answer(
                "🟢 **Kill Switch Deactivated**\n\nProtective actions can now be proposed and executed."
            )
        else:
            self.actions_manager.activate_kill_switch(user.telegram_id)
            await message.answer(
                "🔴 **Kill Switch Activated**\n\n"
                "All pending actions cancelled. No new actions will execute.\n"
                "Use /killswitch again to deactivate."
            )

    async def callback_action_confirm(self, callback: CallbackQuery) -> None:
        """Handle action confirmation/cancellation callbacks."""
        parts = callback.data.split(":")
        action_type = parts[1]
        user_id = callback.from_user.id

        if action_type == "dismiss":
            await callback.message.delete()
            await callback.answer("Dismissed")
            return

        if action_type == "propose":
            symbol = parts[2]
            user = await self.storage.get_user(user_id)
            if not user:
                await callback.answer("User not found")
                return

            positions, _ = await self._get_positions_for_user(user)
            position = next((p for p in positions if p.symbol == symbol), None)

            if not position:
                await callback.answer("Position not found")
                return

            advice = AdviceCalculator.calculate_margin_to_add(position)
            action, msg = self.actions_manager.propose_action(user_id, advice, position)

            if action:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Confirm", callback_data=f"action:confirm:{action.action_id}"),
                        InlineKeyboardButton(text="❌ Cancel", callback_data=f"action:cancel:{action.action_id}"),
                    ]
                ])

                await callback.message.answer(
                    f"**Action Proposed**\n\n"
                    f"Add **${advice.margin_to_add_usd:,.2f}** margin to {symbol}\n"
                    f"→ Distance improves to {advice.target_distance_pct:.0f}%\n\n"
                    f"⚠️ Testnet only. Tap confirm to execute.",
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )
            else:
                await callback.message.answer(f"❌ {msg}")

            await callback.answer()
            return

        action_id = parts[2]

        if action_type == "confirm":
            success, msg = self.actions_manager.confirm_action(action_id, user_id)
            if success:
                exec_success, exec_msg = await self.actions_manager.execute_action(action_id)
                if exec_success:
                    await callback.message.edit_text(f"✅ Action completed: {exec_msg}")
                else:
                    await callback.message.edit_text(f"⚠️ Execution: {exec_msg}")
            else:
                await callback.message.edit_text(f"❌ {msg}")

        elif action_type == "cancel":
            success, msg = self.actions_manager.cancel_action(action_id, user_id)
            await callback.message.edit_text(f"{'✅' if success else '❌'} {msg}")

        await callback.answer()

    async def callback_risk(self, callback: CallbackQuery) -> None:
        """Handle risk overview callbacks."""
        action = callback.data.split(":")[1]
        user_id = callback.from_user.id
        
        user = await self.storage.get_user(user_id)
        if not user:
            await callback.answer("User not found")
            return
        
        if action == "refresh":
            positions, _ = await self._get_positions_for_user(user)
            
            if not positions:
                await callback.answer("No positions found")
                return
            
            analyzer = PortfolioAnalyzer(user.threshold)
            overview = analyzer.analyze_portfolio(positions)
            text = await self._risk_overview_text(overview, user.language)
            
            buttons = []
            if overview.riskiest_position and overview.riskiest_position.margin_advice:
                adv = overview.riskiest_position.margin_advice
                buttons.append([
                    InlineKeyboardButton(
                        text=f"💰 Add ${adv.margin_to_add_usd:.0f} to {overview.riskiest_position.symbol}",
                        callback_data=f"action:propose:{overview.riskiest_position.symbol}"
                    )
                ])
            buttons.append([
                InlineKeyboardButton(text="📋 Detailed Positions", callback_data="risk:positions"),
                InlineKeyboardButton(text="🔄 Refresh", callback_data="risk:refresh"),
            ])
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
            await callback.answer("Refreshed")
        
        elif action == "positions":
            positions, venue_label = await self._get_positions_for_user(user)
            
            if not positions:
                await callback.answer("No positions found")
                return
            
            assessments = self.risk_engine.assess_positions(positions)
            response = f"**{venue_label}**\n\n"
            
            for assessment in assessments:
                pos = assessment.position
                risk_emoji = {"SAFE": "✅", "WARNING": "⚠️", "CRITICAL": "🚨", "EMERGENCY": "🆘"}.get(
                    assessment.risk_level_str, "❓"
                )
                
                response += (
                    f"{risk_emoji} **{pos.symbol}** {pos.side.value.upper()}\n"
                    f"  Distance: {assessment.distance_to_liquidation_pct:.1f}% | "
                    f"PnL: ${pos.unrealized_pnl:,.2f}\n"
                )
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📊 Back to Overview", callback_data="risk:refresh")],
            ])
            
            await callback.message.edit_text(response, parse_mode="Markdown", reply_markup=keyboard)
            await callback.answer()

    async def handle_photo(self, message: Message, state: FSMContext) -> None:
        """Handle photo messages for screenshot parsing with confirmation."""
        user = await self.get_or_create_user(message)
        status_msg = await message.answer("🔍 Analyzing screenshot...")

        try:
            photo = message.photo[-1]
            file = await message.bot.get_file(photo.file_id)
            file_data = await message.bot.download_file(file.file_path)
            image_bytes = file_data.read()

            parsed = await self.llm.parse_screenshot(image_bytes)

            if not parsed:
                await status_msg.edit_text(
                    "❌ Could not parse position from screenshot.\n"
                    "Please make sure the screenshot shows a trading position clearly."
                )
                return

            self._pending_screenshots[message.from_user.id] = parsed

            confidence = parsed.get("confidence", 0.5)
            confidence_emoji = "🟢" if confidence > 0.8 else "🟡" if confidence > 0.5 else "🔴"

            response = (
                f"📊 **Parsed Position** {confidence_emoji}\n\n"
                f"Symbol: **{parsed.get('symbol', 'Unknown')}**\n"
                f"Side: {parsed.get('side', 'Unknown')}\n"
                f"Size: {parsed.get('size', 'Unknown')}\n"
                f"Entry: ${parsed.get('entry_price', 0):,.2f}\n"
                f"Mark: ${parsed.get('mark_price', 0):,.2f}\n"
                f"Liq: ${parsed.get('liquidation_price', 0):,.2f}\n"
                f"Leverage: {parsed.get('leverage', 'Unknown')}x\n"
                f"Venue: {parsed.get('venue', 'Unknown')}\n"
                f"Confidence: {confidence:.0%}\n\n"
                f"{'⚠️ Mock data (demo mode)' if parsed.get('mock') else ''}"
            )

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Confirm & Monitor", callback_data="screenshot:confirm"),
                    InlineKeyboardButton(text="❌ Discard", callback_data="screenshot:discard"),
                ],
                [
                    InlineKeyboardButton(text="✏️ Edit (coming soon)", callback_data="screenshot:edit"),
                ]
            ])

            await status_msg.edit_text(response, reply_markup=keyboard, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error parsing screenshot: {e}")
            await status_msg.edit_text(f"❌ Error: {str(e)}")

    async def callback_screenshot_confirm(self, callback: CallbackQuery, state: FSMContext) -> None:
        """Handle screenshot confirmation callbacks."""
        action = callback.data.split(":")[1]
        user_id = callback.from_user.id

        if action == "discard":
            self._pending_screenshots.pop(user_id, None)
            await callback.message.edit_text("Screenshot discarded.")
            await callback.answer()
            return

        if action == "edit":
            await callback.answer("Edit feature coming soon!", show_alert=True)
            return

        if action == "confirm":
            parsed = self._pending_screenshots.pop(user_id, None)
            if not parsed:
                await callback.answer("No pending screenshot")
                return

            await self.storage.set_demo_mode(user_id, True)

            connector = self.get_demo_connector(user_id)
            connector.add_custom_position(
                symbol=parsed.get("symbol", "BTCUSDT"),
                side=Side.LONG if parsed.get("side") == "long" else Side.SHORT,
                size=float(parsed.get("size", 0.1)),
                entry_price=float(parsed.get("entry_price", 65000)),
                mark_price=float(parsed.get("mark_price", 65000)),
                leverage=float(parsed.get("leverage", 10)),
                margin=float(parsed.get("margin", 650)),
            )

            await callback.message.edit_text(
                "✅ **Position Added to Monitoring**\n\n"
                f"Symbol: {parsed.get('symbol')}\n"
                f"Demo mode enabled. Use /positions to view.\n\n"
                "⚠️ Mark price will be simulated. For real monitoring, bind your exchange.",
                parse_mode="Markdown"
            )
            await callback.answer("Position added!")
