"""
PayPal Subscriptions integration for LiqGuard Pro.

Free tier: basic alerts, limited Muse suggestions.
Pro tier (via PayPal Subscriptions): faster polling, multi-account, unlimited Muse rescue advice.
"""

from .agent import AgentCheckout, intent_from_text, parse_grid_filter, run_checkout
from .entitlements import EntitlementService, ProStatus, Tier
from .paypal_client import PayPalClient, PayPalError

__all__ = [
    "AgentCheckout",
    "EntitlementService",
    "PayPalClient",
    "PayPalError",
    "ProStatus",
    "Tier",
    "intent_from_text",
    "parse_grid_filter",
    "run_checkout",
]
