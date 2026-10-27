"""Build the right adapter for a stored binding."""

from __future__ import annotations

from liqguard.models import ExchangeBinding

from .base import ExchangeAdapter
from .binance import BinanceAdapter
from .hyperliquid import HyperliquidAdapter


class UnsupportedBinding(ValueError):
    pass


def adapter_for_binding(binding: ExchangeBinding) -> ExchangeAdapter:
    if binding.venue == "binance":
        if not (binding.api_key and binding.api_secret):
            raise UnsupportedBinding("Binance binding has no usable credentials")
        return BinanceAdapter(binding.api_key, binding.api_secret, network=binding.network)
    if binding.venue == "hyperliquid":
        if not binding.wallet_address:
            raise UnsupportedBinding("Hyperliquid binding has no wallet address")
        return HyperliquidAdapter(binding.wallet_address)
    raise UnsupportedBinding(f"Unsupported venue {binding.venue!r}")
