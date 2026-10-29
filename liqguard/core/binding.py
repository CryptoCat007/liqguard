"""
Self-serve exchange binding shared by the Telegram 🔑 綁定帳號 flow and the
web dashboard. Credentials only travel inward: they are checked against the
exchange, encrypted with the owner's own data key by the storage layer and
never returned, logged or echoed (only masked hints like `…ABCD`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from liqguard.exchanges import BinanceAdapter, HyperliquidAdapter
from liqguard.models import MAINNET, TESTNET, ExchangeBinding, User

logger = logging.getLogger(__name__)

API_TOKEN = re.compile(r"^[A-Za-z0-9]{20,128}$")
WALLET = re.compile(r"^0x[a-fA-F0-9]{40}$")
PRIVATE_KEY = re.compile(r"^(0x)?[a-fA-F0-9]{64}$")
TRADE_OR_WITHDRAW = ("TRADE", "WITHDRAW", "TRANSFER", "MARGIN", "UNIVERSAL")

SECURITY_NOTE = (
    "🔒 安全須知\n"
    "• 只接受「唯讀」API 金鑰：有交易、提幣或轉帳權限的金鑰一律拒絕\n"
    "• 絕對不要傳送私鑰、助記詞（seed phrase）或交易所登入密碼\n"
    "• Hyperliquid 只需要錢包地址（0x 開頭 40 碼），不需要任何金鑰\n"
    "• 金鑰以你專屬的資料金鑰加密保存，畫面上只顯示末四碼"
)


@dataclass(frozen=True)
class BindResult:
    ok: bool
    code: str
    message: str
    hint: Optional[str] = None
    permissions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "code": self.code, "message": self.message, "hint": self.hint, "permissions": self.permissions}


def looks_like_secret_material(text: str, bare_hex: bool = True) -> bool:
    """
    A wallet private key or a 12–24-word seed phrase: never accept, warn loudly.
    `bare_hex=False` only flags `0x`-prefixed keys (Binance API keys can be all-hex).
    """
    t = (text or "").strip()
    words = t.split()
    if len(words) in (12, 15, 18, 21, 24) and all(w.isalpha() and w.islower() for w in words):
        return True
    return bool(PRIVATE_KEY.match(t)) and (bare_hex or t.lower().startswith("0x"))


def split_key_secret(text: str) -> tuple[Optional[str], Optional[str]]:
    """`KEY SECRET` (space or newline separated) → both, a single token → (key, None)."""
    parts = (text or "").split()
    if len(parts) == 1:
        return parts[0], None
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, None


def masked_bindings(user: Optional[User]) -> list[dict]:
    if user is None:
        return []
    return [b.to_dict() for b in user.bindings if b.is_active]


class BindingService:
    def __init__(self, storage, binance_factory: Callable = BinanceAdapter, hyperliquid_factory: Callable = HyperliquidAdapter,
                 default_threshold: Optional[Callable] = None):
        self.storage = storage
        self.binance_factory = binance_factory
        self.hyperliquid_factory = hyperliquid_factory
        self.default_threshold = default_threshold

    async def _ensure_user(self, user_id: int) -> User:
        user = await self.storage.get_user(user_id)
        if user is None:
            user = User(telegram_id=user_id)
            if self.default_threshold is not None:
                user.threshold = self.default_threshold()
            await self.storage.save_user(user)
        return user

    async def bind_binance(self, user_id: int, api_key: str, api_secret: str, network: str = MAINNET) -> BindResult:
        api_key, api_secret = (api_key or "").strip(), (api_secret or "").strip()
        if looks_like_secret_material(api_key, bare_hex=False) or looks_like_secret_material(api_secret, bare_hex=False):
            return BindResult(False, "secret_material", "⛔ 這看起來像私鑰或助記詞。請立刻妥善保管，絕對不要傳給任何人。LiqGuard 只需要唯讀 API 金鑰。")
        if not API_TOKEN.match(api_key) or not API_TOKEN.match(api_secret):
            return BindResult(False, "invalid_format", "❌ 格式不正確：API Key 與 Secret 應各為 20–128 個英數字元。")
        if api_key == api_secret:
            return BindResult(False, "invalid_format", "❌ API Key 與 Secret 不能相同，請確認兩者都複製正確。")
        if network not in (MAINNET, TESTNET):
            return BindResult(False, "invalid_format", "❌ 不支援的網路。")
        try:
            async with self.binance_factory(api_key, api_secret, network=network) as adapter:
                check = await adapter.check_key()
        except Exception as e:
            logger.error("Binance key check failed for user %s: %s", user_id, type(e).__name__)
            return BindResult(False, "error", "⚠️ 暫時無法連線到 Binance 驗證金鑰，請稍後再試。")
        if not check.accepted:
            risky = [p for p in check.permissions if any(t in p.upper() for t in TRADE_OR_WITHDRAW)]
            if risky and check.verified:
                return BindResult(False, "rejected_permissions",
                                  f"🚫 已拒絕：這把金鑰有 {', '.join(risky)} 權限。請建立只勾選「Enable Reading（允許讀取）」的新金鑰。",
                                  permissions=check.permissions)
            if "Invalid" in check.message:
                return BindResult(False, "invalid_key", "❌ API Key 或 Secret 錯誤，請重新複製後再試。")
            return BindResult(False, "rejected", f"🚫 未通過驗證：{check.message}", permissions=check.permissions)
        await self._ensure_user(user_id)
        binding = ExchangeBinding(venue="binance", binding_type="api_key", network=network, api_key=api_key,
                                  api_secret=api_secret, permissions=check.permissions)
        await self.storage.save_binding(user_id, binding)
        label = "Binance 測試網" if network == TESTNET else "Binance"
        return BindResult(True, "ok", f"✅ 已綁定 {label}（金鑰 {binding.api_key_hint}），權限：{', '.join(check.permissions) or '唯讀'}",
                          hint=binding.api_key_hint, permissions=check.permissions)

    async def bind_hyperliquid(self, user_id: int, address: str) -> BindResult:
        address = (address or "").strip()
        if looks_like_secret_material(address):
            return BindResult(False, "secret_material", "⛔ 這看起來像私鑰或助記詞，不是錢包地址！請勿外流。Hyperliquid 只需要 0x 開頭、40 碼的公開地址。")
        if not WALLET.match(address):
            return BindResult(False, "invalid_format", "❌ 錢包地址格式不正確：應為 0x 開頭加 40 個十六進位字元。")
        address = address.lower()
        try:
            async with self.hyperliquid_factory(address) as adapter:
                check = await adapter.check_key()
        except Exception as e:
            logger.error("Hyperliquid check failed for user %s: %s", user_id, type(e).__name__)
            return BindResult(False, "error", "⚠️ 暫時無法連線到 Hyperliquid，請稍後再試。")
        if not check.accepted:
            return BindResult(False, "rejected", f"❌ 無法使用這個地址：{check.message}")
        await self._ensure_user(user_id)
        binding = ExchangeBinding(venue="hyperliquid", binding_type="wallet", wallet_address=address, permissions=check.permissions)
        await self.storage.save_binding(user_id, binding)
        hint = binding.to_dict()["wallet_address"]
        return BindResult(True, "ok", f"✅ 已綁定 Hyperliquid 錢包 {hint}（只讀取公開資料）", hint=hint)

    async def list(self, user_id: int) -> list[dict]:
        return masked_bindings(await self.storage.get_user(user_id))

    async def unbind(self, user_id: int, venue: str, network: Optional[str] = None) -> bool:
        if venue not in ("binance", "hyperliquid"):
            return False
        return await self.storage.delete_binding(user_id, venue, network)
