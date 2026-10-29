"""📊 我的倉位: list, detail, and per-position tools 救倉試算 / 插針壓測 / 止損檢查 / 資金費影響."""

from __future__ import annotations

from .. import keyboards as kb
from ..screen import Request, Screen
from ..texts import (
    NO_ACCOUNT,
    NO_POSITIONS,
    NOT_FOUND,
    RISK_EMOJI,
    SEVERITY_ZH,
    demo_tag,
    funding_lines,
    label_zh,
    money,
    pct,
    price,
    side_zh,
)

STOP_ZH = {
    "ok": "✅ 止損有效，會在強平前觸發",
    "partial": "⚠️ 止損只覆蓋部分倉位",
    "no_stop": "🛑 沒有設定止損",
    "beyond_liquidation": "🛑 止損價在強平價之外，永遠不會觸發",
    "too_close": "⚠️ 止損離強平太近，插針時可能來不及",
}
OPTION_ZH = {"add_margin": "💰 追加保證金", "reduce": "✂️ 減倉", "hedge": "🛡 對沖"}


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


def _option_line(o: dict, recommended: str) -> str:
    star = " ⭐ 建議" if o["kind"] == recommended else ""
    params, costs = o.get("params", {}), o["costs"]
    if o["kind"] == "add_margin":
        what = f"追加 {money(params.get('amount_usd'))}"
    elif o["kind"] == "reduce":
        what = f"減倉 {params.get('reduce_pct', 0):g}%"
    else:
        what = f"在 {params.get('hedge_venue', '').title()} 開 {params.get('hedge_size', 0):g} 反向對沖"
    after = "不會再被強平" if o.get("no_liquidation") else f"距強平 → {pct(o.get('new_distance_pct'))}"
    if not o.get("protects_liquidation"):
        after += "（無法避免強平）"
    return (f"{OPTION_ZH.get(o['kind'], o['kind'])}{star}\n"
            f"   {what} · {after}\n"
            f"   成本 {money(costs['total_cost_usd'])}（手續費 {money(costs['fees_usd'])}、"
            f"已實現虧損 {money(costs['realized_loss_usd'])}）· 需資金 {money(costs['capital_required_usd'])}")


async def rescue(req: Request) -> Screen:
    found = await _found(req)
    if found is None:
        return _gone(req)
    plan = req.services.rescue_plan(found)
    if plan is None:
        return _gone(req)
    data = plan.to_dict()
    cur = data["current"]
    head = (f"🛟 救倉試算 · {found.position.symbol} {side_zh(data['side'])}{demo_tag(found.source)}\n\n"
            f"目前距強平 {pct(cur['distance_pct'])}，強平價 {price(cur['price'])}\n"
            f"目標：回到距強平 {data['target_distance_pct']:g}%\n")
    if data.get("already_safe"):
        body = "\n✅ 已經在安全距離內，不需要動作。"
    else:
        body = "\n" + "\n\n".join(_option_line(o, data["recommended"]) for o in data["options"])
        body += "\n\n（成本含手續費與 24 小時資金費估算；LiqGuard 只試算，不會替你下單）"
    return Screen(head + body, kb.position_tools(req.sign, found.source, found.position.symbol, current="pr"))


async def wick_stress(req: Request) -> Screen:
    found = await _found(req)
    if found is None:
        return _gone(req)
    info = req.services.protection(found)
    wicks = info.get("wicks") or {}
    lines = [f"🕯 插針壓測 · {found.position.symbol}{demo_tag(found.source)}", "",
             f"目前距強平 {pct(info.get('distance_pct'))}"]
    exposures = [e for e in wicks.get("exposures", []) if e.get("max_pct") is not None]
    if not exposures:
        lines.append("\n暫無這個標的的 K 線插針資料。")
    for e in exposures:
        away = "已在範圍內" if not e.get("max_wicks_away") else f"還差 {e['max_wicks_away']:.1f} 根最大插針"
        lines.append(f"• {e['interval']}：典型插針 {e['typical_pct']:.2f}% · 最大 {e['max_pct']:.2f}%（{e.get('window_days', 30):g} 天）→ {away}")
    if wicks.get("one_max_wick_liquidates"):
        lines.append("\n⚠️ 只要一根近期最大插針就會觸發強平！建議追加保證金或減倉。")
    elif exposures:
        lines.append("\n✅ 近期最大插針不會直接打到強平價。")
    t = (info.get("thresholds") or {})
    if t.get("adapted"):
        e = t["effective"]
        lines.append(f"\n📈 自適應門檻：警告 {e['warning']:.1f}% · 嚴重 {e['critical']:.1f}% · 緊急 {e['emergency']:.1f}%")
    return Screen("\n".join(lines), kb.position_tools(req.sign, found.source, found.position.symbol, current="pw"))


async def stop_check(req: Request) -> Screen:
    found = await _found(req)
    if found is None:
        return _gone(req)
    stop = req.services.protection(found).get("stop")
    lines = [f"🛑 止損檢查 · {found.position.symbol}{demo_tag(found.source)}", ""]
    if stop is None:
        lines.append("無法讀取這個倉位的委託單，暫時無法檢查止損。")
    else:
        lines.append(STOP_ZH.get(stop["status"], stop["status"]))
        if stop.get("stop_price") is not None:
            lines.append(f"止損價：{price(stop['stop_price'])}")
        if stop.get("gap_pct") is not None:
            lines.append(f"止損與強平間距：{pct(stop['gap_pct'], 2)}（至少需要 {pct(stop['required_gap_pct'], 2)}）")
        lines.append(f"強平價：{price(stop.get('liquidation_price'))}")
        if stop.get("loss_if_liquidated_usd"):
            lines.append(f"若被強平損失約：{money(stop['loss_if_liquidated_usd'])}")
        move = next((f for f in stop.get("fixes", []) if f["kind"] == "move_stop"), None)
        if stop.get("needs_warning") and move:
            lines.append(f"\n💡 建議把止損設在 {price(move['stop_price'])} 或更保守")
        elif stop["status"] == "no_stop":
            lines.append("\n💡 建議設定一張觸發價在強平價之前的止損單")
    return Screen("\n".join(lines), kb.position_tools(req.sign, found.source, found.position.symbol, current="ps"))


async def funding_impact(req: Request) -> Screen:
    found = await _found(req)
    if found is None:
        return _gone(req)
    p, c = found.position, found.snapshot.context
    lines = [f"💸 資金費影響 · {p.symbol} {side_zh(p.side.value)}{demo_tag(found.source)}", ""]
    funding = funding_lines(p, c)
    lines += funding or ["這個倉位暫無資金費率資料。"]
    notional = abs(p.size) * p.mark_price
    lines.append(f"\n平倉手續費（taker {c.fees.taker_rate(p.venue) * 100:.3f}%）：約 {money(c.fees.taker_fee(p.venue, notional))}")
    return Screen("\n".join(lines), kb.position_tools(req.sign, found.source, p.symbol, current="pf"))


HANDLERS = {
    "pl": list_positions,
    "pd": detail,
    "pr": rescue,
    "pw": wick_stress,
    "ps": stop_check,
    "pf": funding_impact,
}
