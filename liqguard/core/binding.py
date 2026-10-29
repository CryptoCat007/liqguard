"""
Self-serve exchange binding shared by the Telegram 🔑 bind-account flow and the
web dashboard (messages in the caller's language). Credentials only travel inward: they are checked against the
exchange, encrypted with the owner's own data key by the storage layer and
never returned, logged or echoed (only masked hints like `…ABCD`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from liqguard.exchanges import BinanceAdapter, HyperliquidAdapter
from liqguard.i18n import EN, t
from liqguard.models import MAINNET, TESTNET, ExchangeBinding, User

logger = logging.getLogger(__name__)

API_TOKEN = re.compile(r"^[A-Za-z0-9]{20,128}$")
WALLET = re.compile(r"^0x[a-fA-F0-9]{40}$")
PRIVATE_KEY = re.compile(r"^(0x)?[a-fA-F0-9]{64}$")
TRADE_OR_WITHDRAW = ("TRADE", "WITHDRAW", "TRANSFER", "MARGIN", "UNIVERSAL")



def security_note(lang: str = EN) -> str:
    return t(lang, "binding.security_note")


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

    async def bind_binance(self, user_id: int, api_key: str, api_secret: str, network: str = MAINNET,
                           lang: str = EN) -> BindResult:
        api_key, api_secret = (api_key or "").strip(), (api_secret or "").strip()
        if looks_like_secret_material(api_key, bare_hex=False) or looks_like_secret_material(api_secret, bare_hex=False):
            return BindResult(False, "secret_material", t(lang, "binding.result.secret_material_key"))
        if not API_TOKEN.match(api_key) or not API_TOKEN.match(api_secret):
            return BindResult(False, "invalid_format", t(lang, "binding.result.key_format"))
        if api_key == api_secret:
            return BindResult(False, "invalid_format", t(lang, "binding.result.key_equals_secret"))
        if network not in (MAINNET, TESTNET):
            return BindResult(False, "invalid_format", t(lang, "binding.result.bad_network"))
        try:
            async with self.binance_factory(api_key, api_secret, network=network) as adapter:
                check = await adapter.check_key()
        except Exception as e:
            logger.error("Binance key check failed for user %s: %s", user_id, type(e).__name__)
            return BindResult(False, "error", t(lang, "binding.result.binance_unreachable"))
        if not check.accepted:
            risky = [p for p in check.permissions if any(w in p.upper() for w in TRADE_OR_WITHDRAW)]
            if risky and check.verified:
                return BindResult(False, "rejected_permissions",
                                  t(lang, "binding.result.rejected_permissions", permissions=", ".join(risky)),
                                  permissions=check.permissions)
            if "Invalid" in check.message:
                return BindResult(False, "invalid_key", t(lang, "binding.result.invalid_key"))
            return BindResult(False, "rejected", t(lang, "binding.result.rejected", reason=check.message),
                              permissions=check.permissions)
        await self._ensure_user(user_id)
        binding = ExchangeBinding(venue="binance", binding_type="api_key", network=network, api_key=api_key,
                                  api_secret=api_secret, permissions=check.permissions)
        await self.storage.save_binding(user_id, binding)
        label = "Binance" + (t(lang, "common.testnet") if network == TESTNET else "")
        perms = ", ".join(check.permissions) or t(lang, "binding.read_only")
        return BindResult(True, "ok", t(lang, "binding.result.binance_ok", label=label, hint=binding.api_key_hint, permissions=perms),
                          hint=binding.api_key_hint, permissions=check.permissions)

    async def bind_hyperliquid(self, user_id: int, address: str, lang: str = EN) -> BindResult:
        address = (address or "").strip()
        if looks_like_secret_material(address):
            return BindResult(False, "secret_material", t(lang, "binding.result.secret_material_wallet"))
        if not WALLET.match(address):
            return BindResult(False, "invalid_format", t(lang, "binding.result.wallet_format"))
        address = address.lower()
        try:
            async with self.hyperliquid_factory(address) as adapter:
                check = await adapter.check_key()
        except Exception as e:
            logger.error("Hyperliquid check failed for user %s: %s", user_id, type(e).__name__)
            return BindResult(False, "error", t(lang, "binding.result.hyperliquid_unreachable"))
        if not check.accepted:
            return BindResult(False, "rejected", t(lang, "binding.result.wallet_rejected", reason=check.message))
        await self._ensure_user(user_id)
        binding = ExchangeBinding(venue="hyperliquid", binding_type="wallet", wallet_address=address, permissions=check.permissions)
        await self.storage.save_binding(user_id, binding)
        hint = binding.to_dict()["wallet_address"]
        return BindResult(True, "ok", t(lang, "binding.result.hyperliquid_ok", hint=hint), hint=hint)

    async def list(self, user_id: int) -> list[dict]:
        return masked_bindings(await self.storage.get_user(user_id))

    async def unbind(self, user_id: int, venue: str, network: Optional[str] = None) -> bool:
        if venue not in ("binance", "hyperliquid"):
            return False
        return await self.storage.delete_binding(user_id, venue, network)
