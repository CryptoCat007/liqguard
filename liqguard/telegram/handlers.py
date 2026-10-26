"""Telegram bot handlers for LiqGuard."""

import logging
import re

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from liqguard.connectors import BinanceConnector, DemoConnector, HyperliquidConnector
from liqguard.llm import MetaLLMClient
from liqguard.models import ExchangeBinding, User
from liqguard.services import AlertManager, EncryptedStorage, RiskEngine

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


class BotHandlers:
    """Handler class for the Telegram bot."""

    def __init__(
        self,
        storage: EncryptedStorage,
        risk_engine: RiskEngine,
        alert_manager: AlertManager,
        llm_client: MetaLLMClient,
    ):
        self.storage = storage
        self.risk_engine = risk_engine
        self.alert_manager = alert_manager
        self.llm = llm_client

        self._demo_connectors: dict[int, DemoConnector] = {}
        self._temp_binance_key: dict[int, str] = {}

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

    def register(self, dp: Dispatcher) -> None:
        """Register all handlers with the dispatcher."""
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

        router.callback_query.register(
            self.callback_venue_selected,
            F.data.startswith("venue:")
        )
        router.callback_query.register(
            self.callback_unbind_venue,
            F.data.startswith("unbind:")
        )
        router.callback_query.register(
            self.callback_demo_scenario,
            F.data.startswith("demo:")
        )
        router.callback_query.register(
            self.callback_threshold_type,
            F.data.startswith("threshold:")
        )
        router.callback_query.register(
            self.callback_language,
            F.data.startswith("lang:")
        )

        router.message.register(
            self.process_binance_api_key,
            BindingStates.waiting_for_binance_api_key
        )
        router.message.register(
            self.process_binance_api_secret,
            BindingStates.waiting_for_binance_api_secret
        )
        router.message.register(
            self.process_hyperliquid_address,
            BindingStates.waiting_for_hyperliquid_address
        )
        router.message.register(
            self.process_threshold_value,
            ThresholdStates.waiting_for_value
        )

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
/demo - 切換演示模式

**AI 功能**
/ask <問題> - 詢問關於您的倉位
📸 發送截圖 - 解析倉位截圖

**安全說明**
• 僅使用只讀 API 金鑰
• 金鑰以加密方式存儲
• 我們從不進行交易或提款
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
/demo - Toggle demo mode

**AI Features**
/ask <question> - Ask about your positions
📸 Send a screenshot - Parse position from image

**Security Notes**
• Only use READ-ONLY API keys
• Keys are stored encrypted
• We never trade or withdraw
"""
        await message.answer(help_text, parse_mode="Markdown")

    async def cmd_bind(self, message: Message, state: FSMContext) -> None:
        """Handle /bind command."""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🟡 Binance Futures", callback_data="venue:binance"),
            ],
            [
                InlineKeyboardButton(text="🟢 Hyperliquid", callback_data="venue:hyperliquid"),
            ],
        ])

        await message.answer(
            "Select the exchange to connect:\n\n"
            "• **Binance**: Requires API key (read-only)\n"
            "• **Hyperliquid**: Only wallet address needed",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    async def callback_venue_selected(
        self,
        callback: CallbackQuery,
        state: FSMContext
    ) -> None:
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

    async def process_binance_api_key(
        self,
        message: Message,
        state: FSMContext
    ) -> None:
        """Process Binance API key input."""
        api_key = message.text.strip()

        if len(api_key) < 20:
            await message.answer("❌ Invalid API key format. Please try again.")
            return

        self._temp_binance_key[message.from_user.id] = api_key
        await state.set_state(BindingStates.waiting_for_binance_api_secret)

        await message.delete()

        await message.answer(
            "✅ API key received (hidden for security).\n\n"
            "Now send your API Secret:",
            parse_mode="Markdown"
        )

    async def process_binance_api_secret(
        self,
        message: Message,
        state: FSMContext
    ) -> None:
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
                    "• ✅ Enable Reading\n"
                    "• ❌ Disable Trading\n"
                    "• ❌ Disable Withdrawals\n\n"
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

    async def process_hyperliquid_address(
        self,
        message: Message,
        state: FSMContext
    ) -> None:
        """Process Hyperliquid wallet address."""
        address = message.text.strip().lower()

        if not re.match(r"^0x[a-f0-9]{40}$", address):
            await message.answer(
                "❌ Invalid wallet address format.\n"
                "Must be a 42-character hex address starting with 0x.\n\n"
                "Please try again:"
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
                f"✅ **Hyperliquid wallet connected!**\n\n"
                f"Address: `{address[:8]}...{address[-6:]}`\n\n"
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
                buttons.append([
                    InlineKeyboardButton(text=label, callback_data=f"unbind:{binding.venue}")
                ])

        if not buttons:
            await message.answer("You have no active connections.")
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(
            "Select exchange to disconnect:",
            reply_markup=keyboard
        )

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

    async def cmd_positions(self, message: Message) -> None:
        """Handle /positions command."""
        user = await self.get_or_create_user(message)

        if user.is_demo_mode:
            connector = self.get_demo_connector(user.telegram_id)
            positions = await connector.get_positions()
            venue_label = "📊 Demo Mode"
        elif not user.has_any_binding():
            await message.answer(
                "No exchanges connected.\n\n"
                "Use /bind to connect an exchange or /demo to try demo mode."
            )
            return
        else:
            positions = []
            venue_label = "📊 Live Positions"

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
                    await message.answer(f"⚠️ Error fetching from {binding.venue}: {str(e)}")

        if not positions:
            await message.answer(
                f"{venue_label}\n\n"
                "No open positions found."
            )
            return

        assessments = self.risk_engine.assess_positions(positions)

        response = f"**{venue_label}**\n\n"

        for assessment in assessments:
            pos = assessment.position

            risk_emoji = {
                "SAFE": "✅",
                "WARNING": "⚠️",
                "CRITICAL": "🚨",
                "EMERGENCY": "🆘",
            }.get(assessment.risk_level_str, "❓")

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
                response += f"└ Add ${assessment.margin_to_safety:,.2f} for {self.risk_engine.threshold.safety_target_pct}% safety\n"
            else:
                response += "└ Position is safe\n"

            response += "\n"

        if len(response) > 4000:
            response = response[:3900] + "\n\n... (truncated)"

        await message.answer(response, parse_mode="Markdown")

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

    async def callback_threshold_type(
        self,
        callback: CallbackQuery,
        state: FSMContext
    ) -> None:
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
            f"**Setting: {labels.get(threshold_type, threshold_type)}**\n\n"
            "Enter the new percentage value (e.g., 15):",
            parse_mode="Markdown"
        )
        await callback.answer()

    async def process_threshold_value(
        self,
        message: Message,
        state: FSMContext
    ) -> None:
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
            f"✅ {threshold_type.title()} threshold updated to {value}%\n\n"
            "Use /threshold to view all settings."
        )

    async def cmd_demo(self, message: Message) -> None:
        """Handle /demo command."""
        user = await self.get_or_create_user(message)

        if user.is_demo_mode:
            await self.storage.set_demo_mode(user.telegram_id, False)
            user.is_demo_mode = False

            if user.telegram_id in self._demo_connectors:
                del self._demo_connectors[user.telegram_id]

            await message.answer(
                "📊 Demo mode **disabled**.\n\n"
                "Use /positions to view your live positions.",
                parse_mode="Markdown"
            )
        else:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="😊 Normal Market", callback_data="demo:normal")],
                [InlineKeyboardButton(text="📈 Volatile Market", callback_data="demo:volatile")],
                [InlineKeyboardButton(text="🚨 Critical Risk", callback_data="demo:critical")],
                [InlineKeyboardButton(text="✅ Safe Portfolio", callback_data="demo:safe")],
            ])

            await message.answer(
                "🎮 **Demo Mode**\n\n"
                "Select a scenario to simulate:\n"
                "• Normal: Typical market conditions\n"
                "• Volatile: High volatility, some risk\n"
                "• Critical: Positions near liquidation\n"
                "• Safe: All positions are safe",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )

    async def callback_demo_scenario(self, callback: CallbackQuery) -> None:
        """Handle demo scenario selection."""
        scenario = callback.data.split(":")[1]
        user_id = callback.from_user.id

        await self.storage.set_demo_mode(user_id, True)
        self._demo_connectors[user_id] = DemoConnector(scenario)

        await callback.message.edit_text(
            f"🎮 Demo mode **enabled** with **{scenario}** scenario.\n\n"
            "Use /positions to view simulated positions.\n"
            "Use /demo again to change scenario or disable.",
            parse_mode="Markdown"
        )
        await callback.answer()

    async def cmd_ask(self, message: Message) -> None:
        """Handle /ask command for AI questions."""
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

        if user.is_demo_mode:
            connector = self.get_demo_connector(user.telegram_id)
            positions = await connector.get_positions()
        elif user.has_any_binding():
            positions = []
            for binding in user.bindings:
                if not binding.is_active:
                    continue
                try:
                    if binding.venue == "binance":
                        conn = BinanceConnector(binding.api_key, binding.api_secret_encrypted)
                    elif binding.venue == "hyperliquid":
                        conn = HyperliquidConnector(binding.wallet_address)
                    else:
                        continue
                    positions.extend(await conn.get_positions())
                    await conn.close()
                except Exception as e:
                    logger.error(f"Error fetching positions: {e}")
        else:
            positions = []

        if not positions:
            await message.answer("No positions to analyze. Connect an exchange or enable demo mode.")
            return

        status_msg = await message.answer("🤔 Thinking...")

        answer = await self.llm.answer_portfolio_question(
            positions, question, user.language
        )

        await status_msg.edit_text(answer)

    async def cmd_language(self, message: Message) -> None:
        """Handle /language command."""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🇺🇸 English", callback_data="lang:en")],
            [InlineKeyboardButton(text="🇹🇼 繁體中文", callback_data="lang:zh-TW")],
        ])

        await message.answer(
            "Select your preferred language:",
            reply_markup=keyboard
        )

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

        status = (
            f"**Account Status**\n\n"
            f"🆔 User ID: `{user.telegram_id}`\n"
            f"🌐 Language: {user.language}\n"
            f"🎮 Demo Mode: {'On' if user.is_demo_mode else 'Off'}\n\n"
            f"**Connected Exchanges:**\n{bindings_info}\n"
            f"**Alert Thresholds:**\n"
            f"  ⚠️ Warning: {user.threshold.warning_distance_pct}%\n"
            f"  🚨 Critical: {user.threshold.critical_distance_pct}%\n"
            f"  🆘 Emergency: {user.threshold.emergency_distance_pct}%\n"
        )

        await message.answer(status, parse_mode="Markdown")

    async def handle_photo(self, message: Message) -> None:
        """Handle photo messages for screenshot parsing."""
        await self.get_or_create_user(message)

        if not self.llm.is_available:
            await message.answer(
                "📸 Screenshot parsing requires AI features.\n"
                "META_API_KEY environment variable not set."
            )
            return

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

            response = (
                f"📊 **Parsed Position:**\n\n"
                f"Symbol: {parsed.get('symbol', 'Unknown')}\n"
                f"Side: {parsed.get('side', 'Unknown')}\n"
                f"Size: {parsed.get('size', 'Unknown')}\n"
                f"Entry: ${parsed.get('entry_price', 0):,.2f}\n"
                f"Mark: ${parsed.get('mark_price', 0):,.2f}\n"
                f"Liq: ${parsed.get('liquidation_price', 0):,.2f}\n"
                f"Leverage: {parsed.get('leverage', 'Unknown')}x\n"
                f"Venue: {parsed.get('venue', 'Unknown')}\n"
            )

            await status_msg.edit_text(response, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error parsing screenshot: {e}")
            await status_msg.edit_text(f"❌ Error: {str(e)}")
