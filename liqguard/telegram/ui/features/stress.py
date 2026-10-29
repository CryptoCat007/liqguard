"""🧪 Stress test: account-level correlated stress (cross wallets) plus links to per-position wick stress."""

from __future__ import annotations

from .. import keyboards as kb
from ..screen import Request, Screen
from ..texts import book_label, money, pct


async def stress(req: Request) -> Screen:
    s, T = req.services, req.T
    user = await s.user(req.user_id)
    snapshot = await s.snapshot(user)
    if not snapshot.label:
        return Screen(T("positions.no_account"), kb.no_account(T, req.sign))
    if not snapshot.positions:
        return Screen(f"{book_label(T, snapshot.label)}\n\n{T('positions.none')}", kb.refresh_back(T, req.sign, "st"))
    insights = s.insights(user, snapshot)
    lines = [T("stress.title"), ""]
    shown = False
    for st in insights.stress:
        d = st.to_dict()
        first = d.get("first_liquidation_move_pct")
        lines.append(T("stress.account", venue=d["venue"].title(), n=len(d["symbols"]), symbols=T.join(d["symbols"])))
        lines.append(T("stress.equity", equity=money(d["equity"]), maintenance=money(d["maintenance"])))
        if first is not None:
            lines.append(T("stress.first_liq", move=pct(abs(first))))
        for shock in d["shocks"]:
            mark = T("stress.liquidated") if shock["liquidated"] else T("stress.left", amount=money(shock["margin_left"]))
            lines.append(T("stress.shock", move=shock["market_move_pct"], result=mark))
        lines.append("")
        shown = True
    if not shown:
        lines.append(T("stress.isolated"))
    assessed = s.assessed(user, snapshot)
    for p, a, _ in assessed[:5]:
        lines.append(T("stress.single", symbol=p.symbol, dist=pct(a.distance_to_liquidation_pct)))
    worst = insights.worst_stress()
    if worst is not None and worst.first_liquidation is not None and assessed:
        single = assessed[0][1].distance_to_liquidation_pct
        if abs(worst.first_liquidation) < single:
            lines.append("\n" + T("stress.earlier", account=pct(abs(worst.first_liquidation)), single=pct(single)))
    targets = [(s.source_of(snapshot, p), p.symbol) for p, _, _ in assessed]
    return Screen("\n".join(lines).strip(), kb.stress(T, req.sign, targets))


HANDLERS = {"st": stress}
