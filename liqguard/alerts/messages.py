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
