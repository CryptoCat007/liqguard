"""🧪 壓力測試: account-level correlated stress (cross wallets) plus links to per-position wick stress."""

from __future__ import annotations

from .. import keyboards as kb
from ..screen import Request, Screen
from ..texts import NO_ACCOUNT, NO_POSITIONS, label_zh, money, pct


async def stress(req: Request) -> Screen:
    s = req.services
    user = await s.user(req.user_id)
    snapshot = await s.snapshot(user)
    if not snapshot.label:
        return Screen(NO_ACCOUNT, kb.no_account(req.sign))
    if not snapshot.positions:
        return Screen(f"{label_zh(snapshot.label)}\n\n{NO_POSITIONS}", kb.refresh_back(req.sign, "st"))
    insights = s.insights(user, snapshot)
    lines = ["🧪 壓力測試", ""]
    shown = False
    for st in insights.stress:
        d = st.to_dict()
        first = d.get("first_liquidation_move_pct")
        lines.append(f"📉 {d['venue'].title()} 全倉錢包（{len(d['symbols'])} 個倉位：{'、'.join(d['symbols'])}）")
        lines.append(f"權益 {money(d['equity'])} · 維持保證金 {money(d['maintenance'])}")
        if first is not None:
            lines.append(f"⚠️ 所有倉位一起不利移動 {pct(abs(first))}，整個帳戶就會被強平")
        for shock in d["shocks"]:
            mark = "💥 強平" if shock["liquidated"] else f"剩 {money(shock['margin_left'])}"
            lines.append(f"  • 市場 {shock['market_move_pct']:+g}% → {mark}")
        lines.append("")
        shown = True
    if not shown:
        lines.append("你的倉位都是逐倉（或只有一個全倉倉位），彼此不共用保證金：")
    assessed = s.assessed(user, snapshot)
    for p, a, _ in assessed[:5]:
        lines.append(f"• {p.symbol}：價格不利移動 {pct(a.distance_to_liquidation_pct)} 即強平")
    worst = insights.worst_stress()
    if worst is not None and worst.first_liquidation is not None and assessed:
        single = assessed[0][1].distance_to_liquidation_pct
        if abs(worst.first_liquidation) < single:
            lines.append(f"\n🧲 一起下跌時，帳戶在 {pct(abs(worst.first_liquidation))} 就爆，比單看最危險倉位的 {pct(single)} 更早。")
    targets = [(s.source_of(snapshot, p), p.symbol) for p, _, _ in assessed]
    return Screen("\n".join(lines).strip(), kb.stress(req.sign, targets))


HANDLERS = {"st": stress}
