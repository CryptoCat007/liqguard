"""Small zh-Hant-TW formatting helpers shared by feature handlers (pure functions)."""

from __future__ import annotations

from liqguard.core.position_tools import funding_lines, money, pct, price, side_zh
from liqguard.core.positions import LABEL_DEMO, LABEL_LIVE, LABEL_SIMULATION

RISK_EMOJI = {"SAFE": "✅", "WARNING": "⚠️", "CRITICAL": "🚨", "EMERGENCY": "🆘"}
SEVERITY_ZH = {"warning": "警告", "critical": "嚴重", "emergency": "緊急", None: "安全"}
VENUE_ZH = {"binance": "Binance", "hyperliquid": "Hyperliquid", "demo": "Demo"}
LABEL_ZH = {LABEL_SIMULATION: "🎬 Demo 模式 · 市場模擬", LABEL_DEMO: "🎮 Demo 模式", LABEL_LIVE: "📡 即時倉位"}

NO_ACCOUNT = (
    "🔌 尚未綁定任何交易所帳號。\n\n"
    "• 點「🔑 綁定帳號」連接 Binance（唯讀 API 金鑰）或 Hyperliquid（只需錢包地址）\n"
    "• 或點「🎬 Demo」先用模擬倉位體驗"
)
NO_POSITIONS = "📭 目前沒有持倉。"
NOT_FOUND = "這個倉位已不存在（可能已平倉或模擬已結束），已為你更新倉位列表。"


def label_zh(label: str) -> str:
    return LABEL_ZH.get(label, label)


def demo_tag(source: str) -> str:
    return "（模擬資料）" if source in ("sim", "demo") else ""


__all__ = [
    "LABEL_ZH", "NOT_FOUND", "NO_ACCOUNT", "NO_POSITIONS", "RISK_EMOJI", "SEVERITY_ZH", "VENUE_ZH",
    "demo_tag", "funding_lines", "label_zh", "money", "pct", "price", "side_zh",
]
