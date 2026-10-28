"""Alert texts for the protection checks (stop-loss, ADL, wick stress, adaptive tiers). Every number comes from `risk`."""

from __future__ import annotations

from typing import Optional

STOP_HEAD = {
    "no_stop": "🛑 **NO STOP-LOSS**",
    "beyond_liquidation": "🛑 **STOP-LOSS NEVER FIRES**",
    "too_close": "⚠️ **STOP-LOSS TOO CLOSE TO LIQUIDATION**",
}


def _usd(x: Optional[float]) -> str:
    return "n/a" if x is None else f"${x:,.2f}"


def format_stop_message(check: dict, explanation: Optional[dict] = None, demo: bool = False) -> str:
    """From `StopCheck.to_dict()` and Muse's `explain_stop_fix` output."""
    head = STOP_HEAD.get(check["status"], "🛑 **STOP-LOSS CHECK**")
    lines = [f"{head}{' · demo' if demo else ''}", "", f"**{check['symbol']}** {check['side'].upper()} on {check['venue'].title()}"]
    liq = _usd(check["liquidation_price"])
    if check["status"] == "no_stop":
        lines.append(f"No stop order. Liquidation at **{liq}** (mark {_usd(check['mark_price'])}).")
    else:
        where = "beyond" if check["status"] == "beyond_liquidation" else "in front of"
        lines.append(
            f"Stop **{_usd(check['stop_price'])}** vs liquidation **{liq}**: {abs(check['gap_pct']):.2f}% {where} it "
            f"(needs ≥ {check['required_gap_pct']:.2f}% in front)."
        )
    if check.get("loss_if_liquidated_usd"):
        lines.append(f"If liquidated you lose at least {_usd(check['loss_if_liquidated_usd'])}.")
    for f in check.get("fixes", []):
        if f["kind"] == "move_stop":
            lines.append(f"• Move the stop to **{_usd(f['stop_price'])}** → PnL at stop {_usd(f['pnl_at_stop_usd'])} incl. fees")
        elif f["kind"] == "reduce":
            lines.append(f"• Keep the stop, reduce **{f['reduce_pct']:.1f}%** (new size {f['new_size']:.4f}) → liq {_usd(f['new_liquidation_price'])}")
        elif f["kind"] == "add_margin":
            lines.append(f"• Keep the stop, add **{_usd(f['add_margin_usd'])}** margin → liq {_usd(f['new_liquidation_price'])}")
    if explanation:
        tag = "Muse (mock)" if explanation.get("mock") else "Muse"
        lines += ["", f"🧠 _{tag}:_ {explanation['headline']} {explanation['explanation']}"]
    lines.append("\n_LiqGuard is read-only: it never places or edits orders._")
    return "\n".join(lines)


def adl_lights(lights: Optional[int], total: int = 5) -> str:
    if lights is None:
        return "○" * total
    return "●" * lights + "○" * (total - lights)


def format_adl_message(symbol: str, venue: str, side: str, adl: dict, pnl: float, demo: bool = False) -> str:
    return "\n".join([
        f"⚡ **ADL WARNING**{' · demo' if demo else ''}",
        "",
        f"**{symbol}** {side.upper()} on {venue.title()}: ADL {adl_lights(adl['lights'])} **{adl['lights']}/5**, "
        f"unrealized PnL **+{_usd(pnl)}**.",
        "When a liquidation exceeds the insurance fund, the exchange auto-deleverages the most profitable, most "
        "leveraged positions on the other side first. At 4–5 lights this position is near the front of that queue.",
        "Options: take partial profit or lower leverage to move down the queue.",
    ])


def wick_line(wicks: Optional[dict]) -> Optional[str]:
    """`≈ 1.8 max 5m wicks (3.6%) · 4.2 typical (1.5%) from liquidation`."""
    if not wicks or not wicks.get("exposures"):
        return None
    parts = []
    for e in wicks["exposures"]:
        if e["max_wicks_away"] is None:
            continue
        parts.append(
            f"{e['interval']}: {e['max_wicks_away']:.1f}× max ({e['max_pct']:.2f}%), "
            f"{e['typical_wicks_away']:.1f}× typical ({e['typical_pct']:.2f}%)"
        )
    if not parts:
        return None
    flag = " ⚠️ one max wick liquidates" if wicks.get("one_max_wick_liquidates") else ""
    src = " · demo" if wicks.get("source") == "demo" else ""
    return f"Wicks before liq — {' · '.join(parts)}{flag}{src}"


def thresholds_line(thresholds: Optional[dict]) -> Optional[str]:
    if not thresholds or not thresholds.get("adapted"):
        return None
    e, b = thresholds["effective"], thresholds["base"]
    reasons = "; ".join(thresholds.get("reasons", []))
    return (f"Adaptive tiers {e['warning']:.1f}/{e['critical']:.1f}/{e['emergency']:.1f}% "
            f"(yours {b['warning']:g}/{b['critical']:g}/{b['emergency']:g}%): {reasons}")


SEVERITY_ICON = {"warning": "⚠️", "critical": "🚨", "emergency": "🆘"}
HANDLE_FIRST_ICON = "🎯"


def _minutes(m: Optional[float]) -> str:
    if m is None:
        return "n/a"
    if m < 120:
        return f"~{m:.0f} min"
    return f"~{m / 60:.1f} h" if m < 72 * 60 else f"~{m / 1440:.0f} d"


def priority_line(priority: Optional[dict], top: Optional[dict]) -> Optional[str]:
    """`🎯 Handle this first (#1 of 3 at risk)` or `#2 of 3 — handle SOLUSDT first`, from `TriageItem.to_dict()`."""
    if not priority or not top or (priority.get("of") or 0) < 2:
        return None
    if priority.get("label"):
        return f"{HANDLE_FIRST_ICON} **{priority['label']}** (#{priority['rank']} of {priority['of']} positions by urgency)"
    return f"Priority #{priority['rank']} of {priority['of']} — {HANDLE_FIRST_ICON} handle **{top['symbol']}** first"


def triage_lines(items: list[dict]) -> list[str]:
    lines = []
    for t in items:
        icon = SEVERITY_ICON.get(t.get("severity") or "", "✅")
        label = f" — {HANDLE_FIRST_ICON} **{t['label']}**" if t.get("label") else ""
        dist = "n/a" if t.get("distance_pct") is None else f"{t['distance_pct']:.1f}%"
        lines.append(
            f"{t['rank']}. {icon} **{t['symbol']}** {t['side'].upper()} · {dist} from liq · "
            f"{_minutes(t.get('minutes_to_liq'))} at typical volatility · {_usd(t.get('loss_at_liq_usd'))} at stake{label}"
        )
    return lines


def format_triage_message(items: list[dict], severity: Optional[str] = None, demo: bool = False) -> str:
    """One consolidated alert for several positions, most urgent first (from `TriageItem.to_dict()`)."""
    icon = SEVERITY_ICON.get(severity or "", "🚨")
    lines = [f"{icon} **{len(items)} POSITIONS AT RISK — HANDLE IN THIS ORDER**{' · demo' if demo else ''}", ""]
    lines += triage_lines(items)
    if items and items[0].get("reason"):
        lines += ["", f"Why {items[0]['symbol']} first: {items[0]['reason']}."]
    lines.append("_Order = time to liquidation at typical volatility, distance vs your tiers, size at stake._")
    return "\n".join(lines)


def sizing_line(check: dict) -> Optional[str]:
    if check.get("status") != "oversized":
        return None
    reasons = "; ".join(check.get("reasons", []))
    return (f"📏 Oversized: {reasons}. Suggested size ≤ **{check['suggested_size']:g}** "
            f"(≈ {_usd(check.get('suggested_notional'))}, cut {check['reduce_pct']:.0f}%)")


def format_sizing_message(checks: list[dict], demo: bool = False) -> str:
    """From `SizingCheck.to_dict()` of the oversized positions."""
    first = checks[0]
    lines = [
        f"📏 **POSITION SIZE TOO LARGE FOR THE ACCOUNT**{' · demo' if demo else ''}",
        "",
        f"Account equity {_usd(first['equity'])} · your limits: lose ≤ {first['max_risk_pct']:g}% per trade at the exit, "
        f"margin ≤ {first['max_margin_pct']:g}% of equity per position.",
    ]
    for c in checks:
        risk = "n/a" if c.get("risk_pct") is None else f"{c['risk_pct']:.1f}%"
        lines.append(
            f"• **{c['symbol']}** {c['side'].upper()} {c['size']:g} ({_usd(c['notional'])}, "
            f"{c['notional_x_equity']:.1f}× equity): loses {_usd(c.get('loss_at_exit_usd'))} = **{risk}** at its "
            f"{c['exit_kind']}, margin {c['margin_pct']:.0f}% → suggested **{c['suggested_size']:g}** "
            f"(≈ {_usd(c.get('suggested_notional'))})"
        )
    lines.append("Change limits with `/settings risk 2` and `/settings maxmargin 25`.")
    return "\n".join(lines)


def stress_lines(stress: dict) -> list[str]:
    """Shock table and account-level liquidation move from `AccountStress.to_dict()`."""
    cells = []
    for s in stress["shocks"]:
        if s["market_move_pct"] < 0:
            cells.append(f"{s['market_move_pct']:+.0f}%: {'💀 LIQUIDATED' if s['liquidated'] else '✅ ' + _usd(s['margin_left']) + ' left'}")
    lines = [f"If everything moves together — {' · '.join(cells)}"]
    down, uni = stress.get("liq_move_down_pct"), stress.get("uniform_liq_move_down_pct")
    if down is not None:
        lines.append(
            f"Account liquidated by a **{down:.1f}%** market drop (coins moving by their beta)"
            + (f", {uni:.1f}% if all move equally" if uni is not None else "")
        )
    return lines


def comparison_line(c: dict) -> Optional[str]:
    """Platform's displayed liq next to LiqGuard's correlated liq, gap highlighted."""
    if c.get("correlated_price") is None or c.get("platform_price") is None:
        return None
    gap = c.get("gap_pct")
    flag = f" ⚠️ **{gap:.1f} pts closer**" if gap is not None and gap >= 1 else ""
    return (f"{c['symbol']}: platform liq {_usd(c['platform_price'])} ({c['platform_distance_pct']:.1f}% away) · "
            f"LiqGuard correlated liq **{_usd(c['correlated_price'])}** ({c['correlated_distance_pct']:.1f}% away){flag}")


def format_correlation_message(stress: Optional[dict], concentration: dict, comparisons: list[dict],
                               severity: Optional[str] = None, demo: bool = False) -> str:
    icon = SEVERITY_ICON.get(severity or "", "🧲")
    lines = [f"{icon} **PORTFOLIO STRESS TEST**{' · demo' if demo else ''}", ""]
    if concentration.get("message"):
        lines += [f"🧲 {concentration['message']}", ""]
    if stress:
        lines.append(f"{stress['venue'].title()} cross account ({len(stress['symbols'])} positions, one wallet):")
        lines += stress_lines(stress)
    shown = [line for line in (comparison_line(c) for c in comparisons) if line]
    if shown:
        lines += ["", "Platform line vs real (correlated) liquidation:"] + [f"• {line}" for line in shown]
        lines.append("_The platform's line assumes every other position stays still. In a fast move they fall together._")
    return "\n".join(lines)
