"""🚨 What first: the shared triage ranking (same `PortfolioInsights.focus` the alerts and dashboard use)."""

from __future__ import annotations

import math

from .. import keyboards as kb
from ..screen import Request, Screen
from ..texts import book_label, pct, severity, side


async def triage(req: Request) -> Screen:
    s, T = req.services, req.T
    user = await s.user(req.user_id)
    snapshot = await s.snapshot(user)
    if not snapshot.label:
        return Screen(T("positions.no_account"), kb.no_account(T, req.sign))
    if not snapshot.positions:
        return Screen(f"{book_label(T, snapshot.label)}\n\n{T('positions.none')}", kb.refresh_back(T, req.sign, "tr"))
    insights = s.insights(user, snapshot)
    source = {(p.venue.value, p.symbol): s.source_of(snapshot, p) for p in snapshot.positions}
    at_risk = insights.at_risk
    if not at_risk:
        nearest = min(insights.triage, key=lambda t: t.distance_pct, default=None)
        text = T("triage.title") + "\n\n" + T("triage.none_at_risk")
        if nearest is not None:
            text += "\n" + T("triage.nearest", symbol=nearest.symbol, dist=pct(nearest.distance_pct))
        return Screen(text, kb.triage(T, req.sign, []))
    focus = insights.focus or {}
    lines = [T("triage.title_ordered"), ""]
    for t in at_risk:
        moves = T("triage.moves", n=t.typical_moves) if not math.isinf(t.typical_moves) else ""
        lines.append(T("triage.row", rank=t.rank, symbol=t.symbol, side=side(T, t.side), severity=severity(T, t.severity),
                       dist=pct(t.distance_pct), moves=moves))
    code = focus.get("basis_code")
    if code:
        lines.append("\n" + T("triage.focus", symbol=focus["symbol"], basis=T(f"triage.basis.{code}")))
    if focus.get("nearest_symbol") and focus["nearest_symbol"] != focus.get("symbol"):
        lines.append(T("triage.nearest_later", symbol=focus["nearest_symbol"], dist=pct(focus.get("nearest_distance_pct"))))
    buttons = [(source.get((t.venue, t.symbol), t.venue), t.symbol, T("triage.button", symbol=t.symbol, rank=t.rank)) for t in at_risk]
    return Screen("\n".join(lines), kb.triage(T, req.sign, buttons))


HANDLERS = {"tr": triage}
