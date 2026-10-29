"""💸 Funding & fees: next funding payment per position and taker fees to close."""

from __future__ import annotations

from .. import keyboards as kb
from ..screen import Request, Screen
from ..texts import book_label, funding_lines, money, side


async def funding_fees(req: Request) -> Screen:
    s, T = req.services, req.T
    user = await s.user(req.user_id)
    snapshot = await s.snapshot(user)
    if not snapshot.label:
        return Screen(T("positions.no_account"), kb.no_account(T, req.sign))
    if not snapshot.positions:
        return Screen(f"{book_label(T, snapshot.label)}\n\n{T('positions.none')}", kb.refresh_back(T, req.sign, "ff"))
    c = snapshot.context
    lines = [T("funding.title"), ""]
    total_close = 0.0
    for p, _, _ in s.assessed(user, snapshot)[: kb.MAX_POSITIONS]:
        fee = c.fees.taker_fee(p.venue, abs(p.size) * p.mark_price)
        total_close += fee
        lines.append(f"{p.symbol} {side(T, p.side.value)}")
        lines += [f"  {line}" for line in (funding_lines(T, p, c) or [T("funding.no_data")])]
        lines.append("  " + T("funding.close_fee", fee=money(fee), rate=c.fees.taker_rate(p.venue) * 100))
        lines.append("")
    lines.append(T("funding.total", fee=money(total_close)))
    lines.append(T("funding.sign_note"))
    return Screen("\n".join(lines), kb.refresh_back(T, req.sign, "ff"))


HANDLERS = {"ff": funding_fees}
