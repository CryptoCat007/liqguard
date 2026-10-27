"""Executes confirmed tier-1 actions with the owning user's testnet binding."""

from __future__ import annotations

from liqguard.exchanges import BinanceAdapter, adapter_for_binding
from liqguard.models import TESTNET
from liqguard.protect import ProtectiveAction
from liqguard.risk import ActionType
from liqguard.storage import EncryptedStorage


class BindingActionExecutor:
    def __init__(self, storage: EncryptedStorage, adapter_factory=adapter_for_binding):
        self.storage = storage
        self._adapter_factory = adapter_factory

    async def execute(self, action: ProtectiveAction) -> tuple[bool, str]:
        if action.position.network != TESTNET:
            return False, "Mainnet execution is disabled; LiqGuard only reads mainnet accounts."
        if action.advice.action_type != ActionType.ADD_MARGIN:
            return False, "Only add-margin is implemented for testnet execution."
        user = await self.storage.get_user(action.user_id)
        binding = user.get_binding(action.position.venue.value, TESTNET) if user else None
        if binding is None:
            return False, "No testnet binding. Use /bind → Binance testnet first."
        async with self._adapter_factory(binding) as adapter:
            if not isinstance(adapter, BinanceAdapter):
                return False, f"Execution is not supported on {binding.venue}."
            amount = action.advice.margin_to_add_usd or action.advice.estimated_cost_usd or 0
            return await adapter.add_isolated_margin(action.advice.symbol, amount)
