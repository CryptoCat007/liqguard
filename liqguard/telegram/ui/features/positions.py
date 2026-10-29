"""📊 我的倉位: list, detail, and per-position tools 救倉試算 / 插針壓測 / 止損檢查 / 資金費影響."""

from __future__ import annotations

from liqguard.core.position_tools import report

from .. import keyboards as kb
from ..screen import Request, Screen
from ..texts import (
    NO_ACCOUNT,
    NO_POSITIONS,
    NOT_FOUND,
    RISK_EMOJI,
    SEVERITY_ZH,
    demo_tag,
    label_zh,
    money,
    pct,
    price,
    side_zh,
)


async def list_positions(req: Request) -> Screen:
    s = req.services
    user = await s.user(req.user_id)
    snapshot = await s.snapshot(user)
    if not snapshot.label:
        return Screen(NO_ACCOUNT, kb.no_account(req.sign))
    if not snapshot.positions:
        return Screen(f"{label_zh(snapshot.label)}\n\n{NO_POSITIONS}", kb.refresh_back(req.sign, "pl"))
    rows, buttons = [], []
    for p, a, _ in s.assessed(user, snapshot)[: kb.MAX_POSITIONS]:
        emoji = RISK_EMOJI.get(a.risk_level_str, "❓")
        rows.append(
            f"{emoji} {p.symbol} {side_zh(p.side.value)} {p.leverage:g}x · 距強平 {pct(a.distance_to_liquidation_pct)}"
            f" · 未實現 {money(p.unrealized_pnl, signed=True)}"
        )
        buttons.append((s.source_of(snapshot, p), p.symbol, f"{emoji} {p.symbol} {side_zh(p.side.value)} · {pct(a.distance_to_liquidation_pct)}"))
    more = len(snapshot.positions) - len(rows)
    text = (
        f"📊 我的倉位 · {label_zh(snapshot.label)}\n（由最危險排起）\n\n" + "\n".join(rows)
        + (f"\n…另有 {more} 個倉位，請到網頁看板查看" if more > 0 else "")
        + "\n\n點倉位名稱看詳情，或直接點下方工具。"
    )
    return Screen(text, kb.positions_list(req.sign, buttons))


async def _found(req: Request):
    source, symbol = req.args
    return await req.services.find(req.user_id, source, symbol)


def _gone(req: Request) -> Screen:
    return Screen(NOT_FOUND, kb.positions_list(req.sign, []), toast="倉位已不存在")


async def detail(req: Request) -> Screen:
    found = await _found(req)
    if found is None:
        return _gone(req)
    s, p = req.services, found.position
    a = next(a for q, a, _ in s.assessed(found.user, found.snapshot) if q is p)
    insights = s.insights(found.user, found.snapshot)
    prio = insights.priority(p)
    c = found.snapshot.context
    true_liq = c.liquidation(p).price if c.computable(p) else None
    lines = [
        f"{RISK_EMOJI.get(a.risk_level_str, '❓')} {p.symbol} {side_zh(p.side.value)} · {p.leverage:g}x · {p.margin_mode}{demo_tag(found.source)}",
        "",
        f"倉位大小：{p.size:g}",
        f"開倉價：{price(p.entry_price)}",
        f"標記價：{price(p.mark_price)}",
        f"強平價（交易所）：{price(p.liquidation_price)}",
    ]
    if true_liq and p.liquidation_price and abs(true_liq - p.liquidation_price) / p.liquidation_price >= 0.0005:
        lines.append(f"強平價（LiqGuard 實算）：{price(true_liq)}")
    lines += [
        f"距強平：{pct(a.distance_to_liquidation_pct)}（{SEVERITY_ZH.get(a.severity.value if a.severity else None)}）",
        f"未實現損益：{money(p.unrealized_pnl, signed=True)}",
    ]
    if prio is not None:
        lines.append(f"處理順序：第 {prio.rank}/{prio.of} 名")
    if a.margin_to_safety > 0:
        lines.append(f"\n💡 追加 {money(a.margin_to_safety)} 保證金可回到距強平 {found.user.threshold.safety_target_pct:g}%")
    return Screen("\n".join(lines), kb.position_tools(req.sign, found.source, p.symbol))


async def _tool(req: Request, tool: str, code: str) -> Screen:
    found = await _found(req)
    if found is None:
        return _gone(req)
    plan = None
    if tool == "rescue":
        built = req.services.rescue_plan(found)
        if built is None:
            return _gone(req)
        plan = built.to_dict()
    data = report(tool, found.position, found.snapshot.context, req.services.protection(found), plan,
                  demo=found.source in ("sim", "demo"))
    return Screen(data["title"] + "\n\n" + "\n".join(data["lines"]).strip(),
                  kb.position_tools(req.sign, found.source, found.position.symbol, current=code))


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
