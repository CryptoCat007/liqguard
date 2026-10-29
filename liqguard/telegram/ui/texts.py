"""Small zh-Hant-TW formatting helpers shared by feature handlers (pure functions)."""

from __future__ import annotations

from typing import Optional

from liqguard.core.positions import LABEL_DEMO, LABEL_LIVE, LABEL_SIMULATION
from liqguard.models import Position
from liqguard.risk import RiskContext, project_funding

RISK_EMOJI = {"SAFE": "✅", "WARNING": "⚠️", "CRITICAL": "🚨", "EMERGENCY": "🆘"}
SEVERITY_ZH = {"warning": "警告", "critical": "嚴重", "emergency": "緊急", None: "安全"}
SIDE_ZH = {"long": "多", "short": "空"}
VENUE_ZH = {"binance": "Binance", "hyperliquid": "Hyperliquid", "demo": "Demo"}
LABEL_ZH = {LABEL_SIMULATION: "🎬 Demo 模式 · 市場模擬", LABEL_DEMO: "🎮 Demo 模式", LABEL_LIVE: "📡 即時倉位"}

NO_ACCOUNT = (
    "🔌 尚未綁定任何交易所帳號。\n\n"
    "• 點「🔑 綁定帳號」連接 Binance（唯讀 API 金鑰）或 Hyperliquid（只需錢包地址）\n"
    "• 或點「🎬 Demo」先用模擬倉位體驗"
)
NO_POSITIONS = "📭 目前沒有持倉。"
NOT_FOUND = "這個倉位已不存在（可能已平倉或模擬已結束），已為你更新倉位列表。"


def money(x: Optional[float], signed: bool = False) -> str:
    if x is None:
        return "—"
    if signed:
        return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"
    return f"${x:,.2f}"


def price(x: Optional[float]) -> str:
    if x is None or x <= 0:
        return "—"
    return f"${x:,.2f}" if x >= 1 else f"${x:,.6f}"


def pct(x: Optional[float], digits: int = 1) -> str:
    return "—" if x is None else f"{round(x, 2):.{digits}f}%"


def label_zh(label: str) -> str:
    return LABEL_ZH.get(label, label)


def side_zh(side: str) -> str:
    return SIDE_ZH.get(str(side).lower(), str(side))


def demo_tag(source: str) -> str:
    return "（模擬資料）" if source in ("sim", "demo") else ""


def funding_lines(position: Position, context: RiskContext) -> Optional[list[str]]:
    """Rate, next settlement payment and liquidation drift at the current rate, or None without funding data."""
    if not context.computable(position):
        return None
    proj = project_funding(position, context.account_for(position), context.funding_for, context.now)
    if proj is None:
        return None
    verb = "支付" if proj.next_payment_usd > 0 else "收取"
    lines = [
        f"費率：{proj.info.rate * 100:+.4f}% / {proj.info.interval_hours:g} 小時",
        f"下次結算：{proj.minutes_to_settlement:.0f} 分鐘後，你將{verb} {money(abs(proj.next_payment_usd))}",
    ]
    drift = "、".join(f"{d.horizon_hours:g}h → {pct(d.distance_pct)}" if d.liquidation_price else f"{d.horizon_hours:g}h → 無強平"
                      for d in proj.drifts)
    if drift:
        lines.append(f"按目前費率，距強平變化：{drift}")
    return lines
