"""
The four per-position quick actions (救倉試算 / 插針壓測 / 止損檢查 / 資金費影響)
as zh-Hant-TW reports. Pure: everything comes in as plain data, so the
Telegram buttons and the web dashboard buttons show exactly the same thing.
"""

from __future__ import annotations

from typing import Optional

from liqguard.models import Position
from liqguard.risk import RiskContext, project_funding

TOOLS = {"rescue": "🛟 救倉試算", "wick": "🕯 插針壓測", "stop": "🛑 止損檢查", "funding": "💸 資金費影響"}
SIDE_ZH = {"long": "多", "short": "空"}
STOP_ZH = {
    "ok": "✅ 止損有效，會在強平前觸發",
    "partial": "⚠️ 止損只覆蓋部分倉位",
    "no_stop": "🛑 沒有設定止損",
    "beyond_liquidation": "🛑 止損價在強平價之外，永遠不會觸發",
    "too_close": "⚠️ 止損離強平太近，插針時可能來不及",
}
OPTION_ZH = {"add_margin": "💰 追加保證金", "reduce": "✂️ 減倉", "hedge": "🛡 對沖"}


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


def side_zh(side: str) -> str:
    return SIDE_ZH.get(str(side).lower(), str(side))


def _option_lines(o: dict, recommended: str) -> list[str]:
    star = " ⭐ 建議" if o["kind"] == recommended else ""
    params, costs = o.get("params", {}), o["costs"]
    if o["kind"] == "add_margin":
        what = f"追加 {money(params.get('amount_usd'))}"
    elif o["kind"] == "reduce":
        what = f"減倉 {params.get('reduce_pct', 0):g}%"
    else:
        what = f"在 {str(params.get('hedge_venue', '')).title()} 開 {params.get('hedge_size', 0):g} 反向對沖"
    after = "不會再被強平" if o.get("no_liquidation") else f"距強平 → {pct(o.get('new_distance_pct'))}"
    if not o.get("protects_liquidation"):
        after += "（無法避免強平）"
    return [
        f"{OPTION_ZH.get(o['kind'], o['kind'])}{star}",
        f"   {what} · {after}",
        f"   成本 {money(costs['total_cost_usd'])}（手續費 {money(costs['fees_usd'])}、已實現虧損 "
        f"{money(costs['realized_loss_usd'])}）· 需資金 {money(costs['capital_required_usd'])}",
    ]


def rescue_lines(plan: Optional[dict]) -> list[str]:
    if plan is None:
        return ["找不到這個倉位的救倉試算。"]
    cur = plan["current"]
    lines = [f"目前距強平 {pct(cur['distance_pct'])}，強平價 {price(cur['price'])}",
             f"目標：回到距強平 {plan['target_distance_pct']:g}%", ""]
    if plan.get("already_safe"):
        return lines + ["✅ 已經在安全距離內，不需要動作。"]
    for o in plan["options"]:
        lines += _option_lines(o, plan["recommended"]) + [""]
    return lines + ["（成本含手續費與 24 小時資金費估算；LiqGuard 只試算，不會替你下單）"]


def wick_lines(info: dict) -> list[str]:
    wicks = info.get("wicks") or {}
    lines = [f"目前距強平 {pct(info.get('distance_pct'))}"]
    exposures = [e for e in wicks.get("exposures", []) if e.get("max_pct") is not None]
    if not exposures:
        lines.append("暫無這個標的的 K 線插針資料。")
    for e in exposures:
        away = "已在範圍內" if not e.get("max_wicks_away") else f"還差 {e['max_wicks_away']:.1f} 根最大插針"
        lines.append(f"• {e['interval']}：典型插針 {e['typical_pct']:.2f}% · 最大 {e['max_pct']:.2f}%"
                     f"（{e.get('window_days', 30):g} 天）→ {away}")
    if wicks.get("one_max_wick_liquidates"):
        lines.append("⚠️ 只要一根近期最大插針就會觸發強平！建議追加保證金或減倉。")
    elif exposures:
        lines.append("✅ 近期最大插針不會直接打到強平價。")
    t = info.get("thresholds") or {}
    if t.get("adapted"):
        e = t["effective"]
        lines.append(f"📈 自適應門檻：警告 {e['warning']:.1f}% · 嚴重 {e['critical']:.1f}% · 緊急 {e['emergency']:.1f}%")
    return lines


def stop_lines(stop: Optional[dict]) -> list[str]:
    if stop is None:
        return ["無法讀取這個倉位的委託單，暫時無法檢查止損。"]
    lines = [STOP_ZH.get(stop["status"], stop["status"])]
    if stop.get("stop_price") is not None:
        lines.append(f"止損價：{price(stop['stop_price'])}")
    if stop.get("gap_pct") is not None:
        lines.append(f"止損與強平間距：{pct(stop['gap_pct'], 2)}（至少需要 {pct(stop['required_gap_pct'], 2)}）")
    lines.append(f"強平價：{price(stop.get('liquidation_price'))}")
    if stop.get("loss_if_liquidated_usd"):
        lines.append(f"若被強平損失約：{money(stop['loss_if_liquidated_usd'])}")
    move = next((f for f in stop.get("fixes", []) if f["kind"] == "move_stop"), None)
    if stop.get("needs_warning") and move:
        lines.append(f"💡 建議把止損設在 {price(move['stop_price'])} 或更保守")
    elif stop["status"] == "no_stop":
        lines.append("💡 建議設定一張觸發價在強平價之前的止損單")
    return lines


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


def close_fee_line(position: Position, context: RiskContext) -> str:
    fee = context.fees.taker_fee(position.venue, abs(position.size) * position.mark_price)
    return f"平倉手續費（taker {context.fees.taker_rate(position.venue) * 100:.3f}%）：約 {money(fee)}"


def report(tool: str, position: Position, context: RiskContext, protection: dict,
           plan: Optional[dict] = None, demo: bool = False) -> dict:
    """{tool, title, lines} for one quick action on one position."""
    if tool not in TOOLS:
        raise ValueError(f"unknown tool {tool!r}")
    tag = "（模擬資料）" if demo else ""
    title = f"{TOOLS[tool]} · {position.symbol} {side_zh(position.side.value)}{tag}"
    if tool == "rescue":
        lines = rescue_lines(plan)
    elif tool == "wick":
        lines = wick_lines(protection)
    elif tool == "stop":
        lines = stop_lines(protection.get("stop"))
    else:
        lines = (funding_lines(position, context) or ["這個倉位暫無資金費率資料。"]) + [close_fee_line(position, context)]
    return {"tool": tool, "symbol": position.symbol, "venue": position.venue.value, "title": title, "lines": lines}
