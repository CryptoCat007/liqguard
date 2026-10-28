"""Telegram text and keyboards for liquidation, funding and rescue data (all numbers come from `liqguard.risk`)."""

from __future__ import annotations

from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from liqguard.models import Position
from liqguard.risk import RescuePlan, RiskContext, project_funding

OPTION_EMOJI = {"add_margin": "💰", "reduce": "✂️", "hedge": "🛡"}
OPTION_LABEL = {"add_margin": "Add margin", "reduce": "Reduce", "hedge": "Hedge"}


def _money(x: Optional[float]) -> str:
    return "—" if x is None else f"${x:,.2f}"


def liquidation_lines(position: Position, context: Optional[RiskContext]) -> list[str]:
    """Exchange vs computed liquidation price, plus what the computed one accounts for."""
    exchange = position.liquidation_price if position.liquidation_price > 0 else None
    if context is None or not context.computable(position):
        return [f"├ Liq: {_money(exchange)}"]
    result = context.liquidation(position)
    if result.margin_mode == "cross":
        detail = (
            f"cross · wallet {_money(result.wallet_balance)}, other PnL {_money(result.other_upnl)}, "
            f"other maint. {_money(result.other_maintenance)}"
        )
    else:
        detail = f"isolated · margin {_money(result.wallet_balance)}"
    if result.estimated_tiers:
        detail += " · default tiers"
    computed = _money(result.price) if result.price else "none (fully collateralised)"
    if exchange is None or (result.price and abs(exchange - result.price) / exchange < 0.0005):
        return [f"├ Liq: {computed} (true, {detail})"]
    return [f"├ Liq (exchange): {_money(exchange)}", f"├ Liq (true): {computed} ({detail})"]


def funding_lines(position: Position, context: Optional[RiskContext]) -> list[str]:
    if context is None or not context.computable(position):
        return []
    projection = project_funding(position, context.account_for(position), context.funding_for, context.now)
    if projection is None:
        return []
    info = projection.info
    verb = "pays" if projection.next_payment_usd > 0 else "receives"
    demo = " (demo rate)" if info.source == "demo" else ""
    lines = [
        f"├ Funding: {info.rate * 100:+.4f}%/{info.interval_hours:g}h{demo} · next in "
        f"{projection.minutes_to_settlement:.0f} min, {verb} {_money(abs(projection.next_payment_usd))}"
    ]
    drift = " · ".join(
        f"{d.horizon_hours:g}h → {d.distance_pct:.1f}%" if d.liquidation_price else f"{d.horizon_hours:g}h → no liq"
        for d in projection.drifts
    )
    lines.append(f"├ Liq distance drift at current rate: {drift}")
    return lines


def format_rescue_text(plan: dict, explanation: Optional[dict], demo: bool = False) -> str:
    current = plan["current"]
    dist = "no liquidation" if current["distance_pct"] is None else f"{current['distance_pct']:.1f}%"
    lines = [
        f"🛟 **Rescue simulator**{' · demo' if demo else ''}",
        f"**{plan['symbol']}** {plan['side'].upper()} on {plan['venue'].title()} ({plan['margin_mode']})",
        f"Mark {_money(plan['mark_price'])} · Liq {_money(current['price'])} · {dist} away · target {plan['target_distance_pct']:.0f}%",
        "",
    ]
    for o in plan["options"]:
        star = " ⭐" if o["kind"] == plan["recommended"] else ""
        new_dist = "no liquidation" if o["no_liquidation"] else f"{o['new_distance_pct']:.1f}%"
        c = o["costs"]
        lines.append(f"{OPTION_EMOJI.get(o['kind'], '•')} **{o['title']}**{star}")
        lines.append(f"   Liq {_money(o['new_liquidation_price'])} → {new_dist}" + ("" if o["protects_liquidation"] else " (unchanged)"))
        lines.append(
            f"   Cost {_money(c['total_cost_usd'])}: fees {_money(c['fees_usd'])}, realized {_money(c['realized_loss_usd'])}, "
            f"24h funding {_money(c['funding_24h_usd'])}"
            + (f" · capital {_money(c['capital_required_usd'])}" if c["capital_required_usd"] else "")
        )
    lines.append(f"\n_Funding {plan['funding_label']}; fees at taker rate._")
    if explanation:
        tag = "Muse (mock)" if explanation.get("mock") else "Muse"
        lines += ["", f"🧠 _{tag}:_ {explanation['headline']}"]
        for t in explanation.get("trade_offs", []):
            pros = "; ".join(t.get("pros", []))
            cons = "; ".join(t.get("cons", []))
            lines.append(f"{OPTION_EMOJI.get(t['option'], '•')} + {pros}\n   − {cons}")
        if explanation.get("caveat"):
            lines.append(f"_{explanation['caveat']}_")
    text = "\n".join(lines)
    return text if len(text) < 4000 else text[:3900] + "\n…"


def rescue_keyboard(source: str, plan: dict, can_propose: bool) -> InlineKeyboardMarkup:
    """One button per option (shows its details) plus the confirm flow for add-margin when available."""
    symbol = plan["symbol"]
    row = [
        InlineKeyboardButton(
            text=f"{OPTION_EMOJI[o['kind']]} {OPTION_LABEL[o['kind']]}" + (" ⭐" if o["kind"] == plan["recommended"] else ""),
            callback_data=f"rescue:{source}:{symbol}:{o['kind']}",
        )
        for o in plan["options"]
    ]
    rows = [row]
    add = next((o for o in plan["options"] if o["kind"] == "add_margin"), None)
    if can_propose and add and add["params"].get("amount_usd"):
        rows.append([InlineKeyboardButton(
            text=f"✅ Propose adding ${add['params']['amount_usd']:,.0f}", callback_data=f"action:propose:{symbol}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_option_detail(plan: dict, kind: str, explanation: Optional[dict]) -> Optional[str]:
    option = next((o for o in plan["options"] if o["kind"] == kind), None)
    if option is None:
        return None
    c = option["costs"]
    new_dist = "no liquidation" if option["no_liquidation"] else f"{option['new_distance_pct']:.2f}%"
    lines = [
        f"{OPTION_EMOJI.get(kind, '•')} **{option['title']}** · {plan['symbol']}",
        f"New liq: {_money(option['new_liquidation_price'])} ({new_dist} from mark)",
        f"Reaches {plan['target_distance_pct']:.0f}% target: {'yes' if option['reaches_target'] else 'no'}",
        f"Fees: {_money(c['fees_usd'])} · Realized loss: {_money(c['realized_loss_usd'])}",
        f"Funding 24h ({plan['funding_label']}): {_money(c['funding_24h_usd'])}",
        f"Capital required: {_money(c['capital_required_usd'])} · Total cost: **{_money(c['total_cost_usd'])}**",
    ]
    lines += [f"• {n}" for n in option["notes"]]
    trade = next((t for t in (explanation or {}).get("trade_offs", []) if t.get("option") == kind), None)
    if trade:
        lines.append(f"\n🧠 {trade['summary']}")
    lines.append("\n_Simulation only; nothing has been executed._")
    return "\n".join(lines)


def plan_summary_line(plan: RescuePlan) -> str:
    best = plan.option(plan.recommended)
    return f"🛟 Cheapest rescue: {best.title} ({_money(best.total_cost_usd)})" if best else ""
