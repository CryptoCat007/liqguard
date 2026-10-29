"""🚨 先處理哪個: the shared triage ranking (same `PortfolioInsights.focus` the alerts and dashboard use)."""

from __future__ import annotations

from .. import keyboards as kb
from ..screen import Request, Screen
from ..texts import NO_ACCOUNT, NO_POSITIONS, SEVERITY_ZH, label_zh, pct, side_zh

BASIS_ZH = {
    "only position": "唯一一個進入警示區的倉位",
    "closest to liquidation": "離強平最近",
    "closest in volatility-adjusted terms": "以波動度換算後離強平最近（波動大的幣，同樣距離更危險）",
    "inside an alert tier": "唯一進入警示門檻的倉位",
    "highest combined urgency": "綜合緊急度最高（時間、門檻、倉位大小）",
}


async def triage(req: Request) -> Screen:
    s = req.services
    user = await s.user(req.user_id)
    snapshot = await s.snapshot(user)
    if not snapshot.label:
        return Screen(NO_ACCOUNT, kb.no_account(req.sign))
    if not snapshot.positions:
        return Screen(f"{label_zh(snapshot.label)}\n\n{NO_POSITIONS}", kb.refresh_back(req.sign, "tr"))
    insights = s.insights(user, snapshot)
    source = {(p.venue.value, p.symbol): s.source_of(snapshot, p) for p in snapshot.positions}
    at_risk = insights.at_risk
    if not at_risk:
        nearest = min(insights.triage, key=lambda t: t.distance_pct, default=None)
        text = "🚨 先處理哪個\n\n✅ 目前沒有倉位進入警示區。"
        if nearest is not None:
            text += f"\n最接近的是 {nearest.symbol}，距強平 {pct(nearest.distance_pct)}。"
        return Screen(text, kb.triage(req.sign, []))
    focus = insights.focus or {}
    lines = ["🚨 先處理哪個（依處理順序）", ""]
    for t in at_risk:
        moves = f" · 約 {t.typical_moves:.1f} 次典型 5 分鐘波動" if t.typical_moves != float("inf") else ""
        lines.append(f"{t.rank}. {t.symbol} {side_zh(t.side)} · {SEVERITY_ZH.get(t.severity, '安全')} · 距強平 {pct(t.distance_pct)}{moves}")
    basis = BASIS_ZH.get(focus.get("basis"), focus.get("basis"))
    if basis:
        lines.append(f"\n🎯 先處理 {focus['symbol']}：{basis}。")
    if focus.get("nearest_symbol") and focus["nearest_symbol"] != focus.get("symbol"):
        lines.append(f"（離強平最近的是 {focus['nearest_symbol']} {pct(focus.get('nearest_distance_pct'))}，但考慮波動與倉位大小後排在後面）")
    buttons = [(source.get((t.venue, t.symbol), t.venue), t.symbol, f"救 {t.symbol}（第 {t.rank} 名）") for t in at_risk]
    return Screen("\n".join(lines), kb.triage(req.sign, buttons))


HANDLERS = {"tr": triage}
