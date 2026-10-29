"""💸 資金費率/手續費: next funding payment per position and taker fees to close."""

from __future__ import annotations

from .. import keyboards as kb
from ..screen import Request, Screen
from ..texts import NO_ACCOUNT, NO_POSITIONS, funding_lines, label_zh, money, side_zh


async def funding_fees(req: Request) -> Screen:
    s = req.services
    user = await s.user(req.user_id)
    snapshot = await s.snapshot(user)
    if not snapshot.label:
        return Screen(NO_ACCOUNT, kb.no_account(req.sign))
    if not snapshot.positions:
        return Screen(f"{label_zh(snapshot.label)}\n\n{NO_POSITIONS}", kb.refresh_back(req.sign, "ff"))
    c = snapshot.context
    lines = ["💸 資金費率/手續費", ""]
    total_close = 0.0
    for p, _, _ in s.assessed(user, snapshot)[: kb.MAX_POSITIONS]:
        fee = c.fees.taker_fee(p.venue, abs(p.size) * p.mark_price)
        total_close += fee
        lines.append(f"{p.symbol} {side_zh(p.side.value)}")
        lines += [f"  {line}" for line in (funding_lines(p, c) or ["暫無資金費率資料"])]
        lines.append(f"  平倉手續費約 {money(fee)}（taker {c.fees.taker_rate(p.venue) * 100:.3f}%）")
        lines.append("")
    lines.append(f"全部平倉手續費合計約 {money(total_close)}")
    lines.append("正費率：多單付給空單；負費率：空單付給多單。")
    return Screen("\n".join(lines), kb.refresh_back(req.sign, "ff"))


HANDLERS = {"ff": funding_fees}
