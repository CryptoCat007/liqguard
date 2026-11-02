"""Small formatting helpers shared by feature handlers (pure functions; every word comes from the locale files)."""

from __future__ import annotations

from liqguard.core.position_tools import funding_lines, money, pct, price, side
from liqguard.core.positions import BOOK_LABEL
from liqguard.i18n import Translator

RISK_EMOJI = {"SAFE": "✅", "WARNING": "⚠️", "CRITICAL": "🚨", "EMERGENCY": "🆘"}


def book_label(T: Translator, label: str) -> str:
    key = BOOK_LABEL.get(label)
    return T(key) if key else label


def severity(T: Translator, value) -> str:
    return T(f"common.severity.{value or 'safe'}")


def demo_tag(T: Translator, source: str) -> str:
    return T("common.demo_data") if source in ("sim", "demo") else ""


__all__ = [
    "RISK_EMOJI", "book_label", "demo_tag", "funding_lines", "money", "pct", "price", "severity", "side",
]
