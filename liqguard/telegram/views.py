"""Telegram text and keyboards for liquidation, funding and rescue data (all numbers come from `liqguard.risk`)."""

from __future__ import annotations

from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from liqguard.alerts import (
    HANDLE_FIRST_ICON,
    comparison_line,
    format_price,
    priority_line,
    recommendation_line,
    sizing_line,
    stress_lines,
    triage_lines,
)
from liqguard.models import Position
from liqguard.risk import PortfolioInsights, RescuePlan, RiskContext, project_funding

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
    ]
    if explanation:
        lines.append(recommendation_line("en", explanation["headline"], not explanation.get("mock", True)))
    lines.append("")
    trades = {t.get("option"): t for t in (explanation or {}).get("trade_offs", [])}
    if plan.get("already_safe"):
        lines.append(f"✅ Already beyond the {plan['target_distance_pct']:.0f}% target; no rescue needed now.")
    for o in ([] if plan.get("already_safe") else plan["options"]):
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
        trade = trades.get(o["kind"])
        if trade:
            if trade.get("pros"):
                lines.append(f"   + {'; '.join(trade['pros'])}")
            if trade.get("cons"):
                lines.append(f"   − {'; '.join(trade['cons'])}")
    lines.append(f"\n_Funding {plan['funding_label']}; fees at taker rate._")
    if explanation and explanation.get("caveat"):
        lines.append(f"_{explanation['caveat']}_")
    text = "\n".join(lines)
    return text if len(text) < 4000 else text[:3900] + "\n…"


def rescue_keyboard(source: str, plan: dict, can_propose: bool) -> InlineKeyboardMarkup:
    """One button per option (shows its details) plus the confirm flow for add-margin when available."""
    symbol = plan["symbol"]
    if plan.get("already_safe"):
        return InlineKeyboardMarkup(inline_keyboard=[])
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
        lines.append(f"\n💡 {trade['summary']}")
    lines.append("\n_Simulation only; nothing has been executed._")
    return "\n".join(lines)


def plan_summary_line(plan: RescuePlan) -> str:
    if plan.already_safe:
        return ""
    best = plan.option(plan.recommended)
    return f"🛟 Cheapest rescue: {best.title} ({_money(best.total_cost_usd)})" if best else ""


STOP_STATUS = {
    "ok": "✅",
    "partial": "⚠️ partial",
    "no_stop": "🛑 none",
    "beyond_liquidation": "🛑 beyond liq (never fires)",
    "too_close": "⚠️ too close to liq",
}


def protection_lines(info: dict) -> list[str]:
    """Wick stress, adaptive tiers, stop-loss and ADL lines from `PositionProtection.to_dict()`."""
    lines = []
    wicks = info.get("wicks")
    if wicks and wicks.get("exposures"):
        parts = [
            f"{e['interval']} {e['max_wicks_away']:.1f}× max ({e['max_pct']:.2f}%) / {e['typical_wicks_away']:.1f}× typical"
            for e in wicks["exposures"] if e["max_wicks_away"] is not None
        ]
        if parts:
            flag = " ⚠️ one max wick liquidates" if wicks.get("one_max_wick_liquidates") else ""
            demo = " (demo)" if wicks.get("source") == "demo" else ""
            lines.append(f"├ Wicks away{demo}: {'; '.join(parts)}{flag}")
    t = info.get("thresholds") or {}
    if t.get("adapted"):
        e = t["effective"]
        lines.append(f"├ Tiers: {e['warning']:.1f}/{e['critical']:.1f}/{e['emergency']:.1f}% · {'; '.join(t['reasons'])}")
    stop = info.get("stop")
    if stop is None:
        lines.append("├ Stop: — (orders not available)")
    else:
        label = STOP_STATUS.get(stop["status"], stop["status"])
        if stop.get("stop_price") is not None:
            gap = f", {stop['gap_pct']:.2f}% before liq" if stop.get("gap_pct") is not None and stop["gap_pct"] > 0 else ""
            lines.append(f"├ Stop: {label} {_money(stop['stop_price'])}{gap}")
        else:
            lines.append(f"├ Stop: {label}")
        move = next((f for f in stop.get("fixes", []) if f["kind"] == "move_stop"), None)
        if stop["needs_warning"] and move:
            lines.append(f"│  ↳ safe stop ≥ {_money(move['stop_price'])} (keeps {stop['required_gap_pct']:.2f}% before liq)")
    adl = info.get("adl") or {}
    if adl.get("supported") and adl.get("lights"):
        warn = " ⚡ profitable: near the front of the ADL queue" if adl["warn"] else ""
        lines.append(f"├ ADL: {'●' * adl['lights']}{'○' * (5 - adl['lights'])} {adl['lights']}/5{warn}")
    elif adl and not adl.get("supported"):
        lines.append("├ ADL: n/a (Hyperliquid publishes no ADL rank)")
    return lines


def protection_summary(infos: list[dict]) -> Optional[str]:
    no_stop = [i["symbol"] for i in infos if (i.get("stop") or {}).get("status") == "no_stop"]
    useless = [i["symbol"] for i in infos if (i.get("stop") or {}).get("status") in ("beyond_liquidation", "too_close")]
    adl = [i["symbol"] for i in infos if (i.get("adl") or {}).get("warn")]
    one_wick = [i["symbol"] for i in infos if (i.get("wicks") or {}).get("one_max_wick_liquidates")]
    parts = []
    if no_stop:
        parts.append(f"🛑 no stop: {', '.join(no_stop)}")
    if useless:
        parts.append(f"🛑 stop beyond/at liq: {', '.join(useless)}")
    if one_wick:
        parts.append(f"🕯 one max wick from liq: {', '.join(one_wick)}")
    if adl:
        parts.append(f"⚡ ADL 4–5/5: {', '.join(adl)}")
    return "\n".join(parts) if parts else None


def insight_lines(insights: PortfolioInsights, position: Position) -> list[str]:
    """Per-position triage rank, platform vs correlated liq and sizing for /positions."""
    lines = []
    priority, top = insights.priority(position), insights.top
    line = priority_line(priority.to_dict() if priority else None, top.to_dict() if top else None)
    if line:
        lines.append(f"├ {line}")
    comparison = insights.comparison(position)
    if comparison is not None and comparison.correlated_market_move_pct is not None:
        c = comparison.to_dict()
        if c.get("gap_pct") is not None and c["gap_pct"] >= 1:
            lines.append(f"├ Correlated liq: **{format_price(c['correlated_price'])}** ({c['correlated_distance_pct']:.1f}% away, "
                         f"platform line {c['platform_distance_pct']:.1f}%) ⚠️ {c['gap_pct']:.1f} pts closer")
    sizing = insights.sizing_for(position)
    if sizing is not None:
        line = sizing_line(sizing.to_dict())
        if line:
            lines.append(f"├ {line}")
    return lines


def insights_section(insights: PortfolioInsights) -> Optional[str]:
    """/risk: priority triage, portfolio stress test, concentration, platform vs correlated liq, sizing."""
    data = insights.to_dict()
    parts = []
    at_risk = [t for t in data["triage"] if t["at_risk"]]
    if at_risk:
        why = (data.get("focus") or {}).get("why")
        parts.append(f"**{HANDLE_FIRST_ICON} Priority triage** (handle in this order)\n" + "\n".join(triage_lines(at_risk))
                     + (f"\n_Why first: {why}._" if why else ""))
    elif data["triage"]:
        parts.append("**🎯 Priority triage**\nNo position is inside an alert tier.")
    for stress in data["stress"]:
        if len(stress["symbols"]) < 2 and stress["liq_move_down_pct"] is None:
            continue
        lines = [f"**📉 Stress test · {stress['venue'].title()} cross ({len(stress['symbols'])} positions)**"]
        lines += stress_lines(stress)
        comps = [c for c in data["comparisons"] if c["symbol"] in stress["symbols"]]
        shown = [line for line in (comparison_line(c) for c in comps) if line]
        if shown:
            lines.append("Platform line vs LiqGuard correlated liq:")
            lines += [f"• {line}" for line in shown]
        parts.append("\n".join(lines))
    conc = data["concentration"]
    if conc.get("flagged"):
        parts.append(f"**🧲 Concentration**\n{conc['message']}")
    oversized = [s for s in data["sizing"] if s["status"] == "oversized"]
    if oversized:
        first = oversized[0]
        lines = [f"**📏 Position sizing** (limits: {first['max_risk_pct']:g}% risk/trade, {first['max_margin_pct']:g}% margin; /settings)"]
        for c in oversized:
            risk = "n/a" if c.get("risk_pct") is None else f"{c['risk_pct']:.1f}%"
            lines.append(f"• {c['symbol']}: {risk} of equity at its {c['exit_kind']} → suggested ≤ **{c['suggested_size']:g}** "
                         f"(cut {c['reduce_pct']:.0f}%)")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else None
