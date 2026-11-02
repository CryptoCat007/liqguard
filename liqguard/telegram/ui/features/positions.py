"""📊 My positions: list, detail, and per-position tools rescue / wick stress / stop check / funding impact."""

from __future__ import annotations

from typing import Optional

from liqguard.core.position_tools import report

from .. import keyboards as kb
from ..screen import Request, Screen
from ..texts import RISK_EMOJI, book_label, demo_tag, money, pct, price, severity, side


async def list_positions(req: Request) -> Screen:
    s, T = req.services, req.T
    user = await s.user(req.user_id)
    snapshot = await s.snapshot(user)
    if not snapshot.label:
        return Screen(T("positions.no_account"), kb.no_account(T, req.sign))
    if not snapshot.positions:
        return Screen(f"{book_label(T, snapshot.label)}\n\n{T('positions.none')}", kb.refresh_back(T, req.sign, "pl"))
    rows, buttons = [], []
    for p, a, _ in s.assessed(user, snapshot)[: kb.MAX_POSITIONS]:
        emoji = RISK_EMOJI.get(a.risk_level_str, "❓")
        rows.append(T("positions.row", emoji=emoji, symbol=p.symbol, side=side(T, p.side.value), leverage=p.leverage,
                      dist=pct(a.distance_to_liquidation_pct), pnl=money(p.unrealized_pnl, signed=True)))
        buttons.append((s.source_of(snapshot, p), p.symbol,
                        f"{emoji} {p.symbol} {side(T, p.side.value)} · {pct(a.distance_to_liquidation_pct)}"))
    more = len(snapshot.positions) - len(rows)
    text = (
        T("positions.title", book=book_label(T, snapshot.label)) + "\n\n" + "\n".join(rows)
        + ("\n" + T("positions.more", n=more) if more > 0 else "")
        + "\n\n" + T("positions.hint")
    )
    return Screen(text, kb.positions_list(T, req.sign, buttons))


async def _found(req: Request):
    source, symbol = req.args
    return await req.services.find(req.user_id, source, symbol)


def _gone(req: Request) -> Screen:
    T = req.T
    return Screen(T("positions.not_found"), kb.positions_list(T, req.sign, []), toast=T("positions.not_found_toast"))


async def detail(req: Request) -> Screen:
    found = await _found(req)
    if found is None:
        return _gone(req)
    s, p, T = req.services, found.position, req.T
    a = next(a for q, a, _ in s.assessed(found.user, found.snapshot) if q is p)
    insights = s.insights(found.user, found.snapshot)
    prio = insights.priority(p)
    c = found.snapshot.context
    true_liq = c.liquidation(p).price if c.computable(p) else None
    lines = [
        f"{RISK_EMOJI.get(a.risk_level_str, '❓')} {p.symbol} {side(T, p.side.value)} · {p.leverage:g}x · {p.margin_mode}{demo_tag(T, found.source)}",
        "",
        T("positions.size", size=p.size),
        T("positions.entry", price=price(p.entry_price)),
        T("positions.mark", price=price(p.mark_price)),
        T("positions.liq_exchange", price=price(p.liquidation_price)),
    ]
    if true_liq and p.liquidation_price and abs(true_liq - p.liquidation_price) / p.liquidation_price >= 0.0005:
        lines.append(T("positions.liq_true", price=price(true_liq)))
    lines += [
        T("positions.distance", dist=pct(a.distance_to_liquidation_pct),
          severity=severity(T, a.severity.value if a.severity else None)),
        T("positions.pnl", pnl=money(p.unrealized_pnl, signed=True)),
    ]
    if prio is not None:
        lines.append(T("positions.priority", rank=prio.rank, of=prio.of))
    if a.margin_to_safety > 0:
        lines.append("\n" + T("positions.add_margin", amount=money(a.margin_to_safety), target=found.user.threshold.safety_target_pct))
    return Screen("\n".join(lines), kb.position_tools(T, req.sign, found.source, p.symbol))


async def _tool(req: Request, tool: str, code: str) -> Screen:
    found = await _found(req)
    if found is None:
        return _gone(req)
    built = None
    if tool == "rescue":
        built = req.services.rescue_plan(found)
        if built is None:
            return _gone(req)
    plan = built.to_dict() if built is not None else None
    protection = req.services.protection_check(found)
    note = req.services.muse.tool_note(tool, req.T.lang, plan=built, check=protection.stop)
    demo = found.source in ("sim", "demo")

    def text(muse: Optional[dict]) -> str:
        data = report(tool, found.position, found.snapshot.context, protection.to_dict(), plan, demo=demo,
                      lang=req.T.lang, muse=muse)
        return data["title"] + "\n\n" + "\n".join(data["lines"]).strip()

    later = None
    if note is not None and note.pending is not None:
        pending = note.pending

        async def later() -> Optional[str]:
            live = await pending
            return text(live) if live else None

    return Screen(text(note.result if note else None),
                  kb.position_tools(req.T, req.sign, found.source, found.position.symbol, current=code), later=later)


async def rescue(req: Request) -> Screen:
    return await _tool(req, "rescue", "pr")


async def wick_stress(req: Request) -> Screen:
    return await _tool(req, "wick", "pw")


async def stop_check(req: Request) -> Screen:
    return await _tool(req, "stop", "ps")


async def funding_impact(req: Request) -> Screen:
    return await _tool(req, "funding", "pf")


HANDLERS = {
    "pl": list_positions,
    "pd": detail,
    "pr": rescue,
    "pw": wick_stress,
    "ps": stop_check,
    "pf": funding_impact,
}
