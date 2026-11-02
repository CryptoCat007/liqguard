"""
Per-feature handlers. Each module exposes `HANDLERS: dict[action, handler]`
and depends only on the shared `keyboards`, `screen`, `texts` and the
services object it receives; features never import each other.
"""

from __future__ import annotations

from . import (
    account,
    alerts,
    ask,
    binding,
    dashboard,
    demo,
    funding,
    language,
    menu,
    positions,
    risk,
    settings,
    stress,
    triage,
)

FEATURES = {
    "menu": menu,
    "language": language,
    "risk": risk,
    "positions": positions,
    "triage": triage,
    "stress": stress,
    "funding": funding,
    "settings": settings,
    "binding": binding,
    "dashboard": dashboard,
    "demo": demo,
    "alerts": alerts,
    "account": account,
    "ask": ask,
}


def dispatch_table() -> dict:
    """action → handler, merged from every feature; an action claimed twice is a wiring bug."""
    table: dict = {}
    for name, module in FEATURES.items():
        for action, handler in module.HANDLERS.items():
            if action in table:
                raise RuntimeError(f"action {action!r} registered by two features")
            table[action] = handler
    return table
