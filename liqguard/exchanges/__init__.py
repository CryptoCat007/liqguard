"""Exchange adapters behind one interface (`ExchangeAdapter`). Depends only on `liqguard.models`."""

from .base import ExchangeAdapter, KeyCheck, ReadOnlyViolation
from .binance import BinanceAdapter
from .demo import SCENARIOS, DemoAdapter
from .factory import UnsupportedBinding, adapter_for_binding
from .hyperliquid import HyperliquidAdapter
from .permissions import evaluate_binance_restrictions

__all__ = [
    "ExchangeAdapter",
    "KeyCheck",
    "ReadOnlyViolation",
    "BinanceAdapter",
    "DemoAdapter",
    "HyperliquidAdapter",
    "SCENARIOS",
    "UnsupportedBinding",
    "adapter_for_binding",
    "evaluate_binance_restrictions",
]
