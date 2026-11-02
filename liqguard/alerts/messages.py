"""
Alert texts for the protection checks (stop-loss, ADL, wick stress, adaptive
tiers, triage, sizing, correlated stress). Every number comes from `risk`;
every word comes from the locale files, in the recipient's language `lang`.
"""

from __future__ import annotations

from typing import Optional

from liqguard.i18n import EN, Msg, has, join, t, tr

SEVERITY_ICON = {"warning": "⚠️", "critical": "🚨", "emergency": "🆘"}
HANDLE_FIRST_ICON = "🎯"


def demo_tag(lang: str, demo: bool) -> str:
    return t(lang, "alert.demo_tag") if demo else ""


def _usd(x: Optional[float], lang: str = EN) -> str:
    return t(lang, "common.na") if x is None else f"${x:,.2f}"


def format_price(x: Optional[float], lang: str = EN) -> str:
    if x is None:
        return t(lang, "common.na")
    return f"${x:,.2f}" if abs(x) >= 100 else f"${x:,.4f}" if abs(x) >= 1 else f"${x:.6g}"


def side_upper(lang: str, side: str) -> str:
    return t(lang, f"common.side_upper.{side}")


def venue_name(lang: str, venue: Optional[str]) -> str:
    return t(lang, f"common.venue.{venue}") if venue else ""


def muse_tag(lang: str, mock: bool) -> str:
    return t(lang, "alert.muse_mock" if mock else "alert.muse")


def recommendation_line(lang: str, text, live: bool) -> str:
    """
    The one recommendation a message leads with: Muse's wording once it
    arrived (`live`), else the rule-based text, marked with a small ✦ Muse tag.
    """
    return t(lang, "alert.recommend", text=str(tr(lang, text)).strip(),
             tag=t(lang, "alert.muse_mark" if live else "alert.muse_mark_mock"))


def advice_recommendation(card: dict) -> dict:
    """
    {"text", "live"} for a simulated advice card: the recommended rescue (Muse's
    headline when it answered), else the add-margin / reduce advice (Muse's
    explanation when it answered). Telegram and the dashboard both show this line.
    """
    plan, explained = card.get("rescue"), card.get("rescue_explanation") or {}
    best = None
    if plan and not plan.get("already_safe"):
        best = next((o for o in plan["options"] if o["kind"] == plan["recommended"]), None)
    if best is not None and explained.get("headline") and not explained.get("mock", True):
        return {"text": explained["headline"], "live": True}
    if card.get("explanation") and not card.get("mock", True):
        return {"text": card["explanation"], "live": True}
    if best is not None:
        return {"text": Msg("sim.rec_cheapest", title=best["title"], cost=f"{best['costs']['total_cost_usd']:,.2f}"),
                "live": False}
    if card.get("margin_to_add_usd"):
        where = Msg("sim.where_cross" if card.get("margin_mode") == "cross" else "sim.where_margin")
        return {"text": Msg("sim.rec_add", amount=f"{card['margin_to_add_usd']:,.2f}", where=where,
                            target=f"{card['target_distance_pct']:.0f}", pct=f"{card.get('reduce_size_pct', 0):.1f}"),
                "live": False}
    return {"text": card.get("explanation") or "", "live": False}


def format_stop_message(check: dict, explanation: Optional[dict] = None, demo: bool = False, lang: str = EN) -> str:
    """From `StopCheck.to_dict()` and Muse's `explain_stop_fix` output."""
    status = check["status"]
    head = t(lang, f"alert.stop.head_{status}") if status in ("no_stop", "beyond_liquidation", "too_close") else t(lang, "alert.stop.head")
    lines = [head + demo_tag(lang, demo), "",
             t(lang, "alert.position_on", symbol=check["symbol"], side=side_upper(lang, check["side"]), venue=venue_name(lang, check["venue"]))]
    liq = _usd(check["liquidation_price"], lang)
    if status == "no_stop":
        lines.append(t(lang, "alert.stop.no_stop", liq=liq, mark=_usd(check["mark_price"], lang)))
    else:
        where = t(lang, "alert.stop.beyond" if status == "beyond_liquidation" else "alert.stop.in_front")
        lines.append(t(lang, "alert.stop.vs", stop=_usd(check["stop_price"], lang), liq=liq, gap=f"{abs(check['gap_pct']):.2f}",
                       where=where, required=f"{check['required_gap_pct']:.2f}"))
    if check.get("loss_if_liquidated_usd"):
        lines.append(t(lang, "alert.stop.loss", loss=_usd(check["loss_if_liquidated_usd"], lang)))
    if explanation and explanation.get("headline"):
        lines.append(recommendation_line(lang, explanation["headline"], not explanation.get("mock", True)))
    for f in check.get("fixes", []):
        if f["kind"] == "move_stop":
            lines.append(t(lang, "alert.stop.fix_move", stop=_usd(f["stop_price"], lang), pnl=_usd(f["pnl_at_stop_usd"], lang)))
        elif f["kind"] == "reduce":
            lines.append(t(lang, "alert.stop.fix_reduce", pct=f"{f['reduce_pct']:.1f}", size=f"{f['new_size']:.4f}",
                           liq=_usd(f["new_liquidation_price"], lang)))
        elif f["kind"] == "add_margin":
            lines.append(t(lang, "alert.stop.fix_margin", amount=_usd(f["add_margin_usd"], lang), liq=_usd(f["new_liquidation_price"], lang)))
    lines.append("\n" + t(lang, "alert.read_only"))
    return "\n".join(lines)


def adl_lights(lights: Optional[int], total: int = 5) -> str:
    if lights is None:
        return "○" * total
    return "●" * lights + "○" * (total - lights)


def format_adl_message(symbol: str, venue: str, side: str, adl: dict, pnl: float, demo: bool = False, lang: str = EN) -> str:
    return "\n".join([
        t(lang, "alert.adl.head") + demo_tag(lang, demo),
        "",
        t(lang, "alert.adl.body", symbol=symbol, side=side_upper(lang, side), venue=venue_name(lang, venue),
          lights=adl_lights(adl["lights"]), n=adl["lights"], pnl=_usd(pnl, lang)),
        t(lang, "alert.adl.explain"),
        t(lang, "alert.adl.options"),
    ])


def wick_line(wicks: Optional[dict], lang: str = EN) -> Optional[str]:
    """`≈ 1.8 max 5m wicks (3.6%) · 4.2 typical (1.5%) from liquidation`."""
    if not wicks or not wicks.get("exposures"):
        return None
    parts = [
        t(lang, "alert.wick.part", interval=e["interval"], max=f"{e['max_wicks_away']:.1f}", max_pct=f"{e['max_pct']:.2f}",
          typical=f"{e['typical_wicks_away']:.1f}", typical_pct=f"{e['typical_pct']:.2f}")
        for e in wicks["exposures"] if e["max_wicks_away"] is not None
    ]
    if not parts:
        return None
    flag = t(lang, "alert.wick.one_liquidates") if wicks.get("one_max_wick_liquidates") else ""
    src = t(lang, "alert.demo_tag") if wicks.get("source") == "demo" else ""
    return t(lang, "alert.wick.line", parts=" · ".join(parts)) + flag + src


def thresholds_line(thresholds: Optional[dict], lang: str = EN) -> Optional[str]:
    if not thresholds or not thresholds.get("adapted"):
        return None
    e, b = thresholds["effective"], thresholds["base"]
    return t(lang, "alert.adaptive", effective=f"{e['warning']:.1f}/{e['critical']:.1f}/{e['emergency']:.1f}",
             base=f"{b['warning']:g}/{b['critical']:g}/{b['emergency']:g}", reasons=join(lang, thresholds.get("reasons", []), "; "))


def _minutes(m: Optional[float], lang: str = EN) -> str:
    if m is None:
        return t(lang, "common.na")
    if m < 120:
        return t(lang, "alert.duration.min", n=f"{m:.0f}")
    return t(lang, "alert.duration.h", n=f"{m / 60:.1f}") if m < 72 * 60 else t(lang, "alert.duration.d", n=f"{m / 1440:.0f}")


def priority_line(priority: Optional[dict], top: Optional[dict], lang: str = EN) -> Optional[str]:
    """`🎯 Handle this first (#1 of 3 at risk)` or `#2 of 3 — handle SOLUSDT first`, from `TriageItem.to_dict()`."""
    if not priority or not top or (priority.get("of") or 0) < 2:
        return None
    if priority.get("label"):
        return t(lang, "alert.priority.first", icon=HANDLE_FIRST_ICON, label=tr(lang, priority["label"]),
                 rank=priority["rank"], of=priority["of"])
    return t(lang, "alert.priority.later", icon=HANDLE_FIRST_ICON, rank=priority["rank"], of=priority["of"], symbol=top["symbol"])


def triage_lines(items: list[dict], lang: str = EN) -> list[str]:
    lines = []
    for item in items:
        icon = SEVERITY_ICON.get(item.get("severity") or "", "✅")
        label = f" — {HANDLE_FIRST_ICON} **{tr(lang, item['label'])}**" if item.get("label") else ""
        dist = t(lang, "common.na") if item.get("distance_pct") is None else f"{item['distance_pct']:.1f}%"
        lines.append(t(lang, "alert.triage.row", rank=item["rank"], icon=icon, symbol=item["symbol"],
                       side=side_upper(lang, item["side"]), dist=dist, time=_minutes(item.get("minutes_to_liq"), lang),
                       notional=f"{item['notional']:,.0f}") + label)
    return lines


def format_triage_message(items: list[dict], severity: Optional[str] = None, demo: bool = False,
                          focus: Optional[dict] = None, lang: str = EN) -> str:
    """One consolidated alert for several positions, most urgent first (from `TriageItem.to_dict()`)."""
    icon = SEVERITY_ICON.get(severity or "", "🚨")
    lines = [t(lang, "alert.triage.head", icon=icon, n=len(items)) + demo_tag(lang, demo), ""]
    lines += triage_lines(items, lang)
    why = None
    if focus and items and focus.get("symbol") == items[0]["symbol"] and focus.get("why"):
        why = focus["why"]
    elif items and items[0].get("reason"):
        why = items[0]["reason"]
    if why:
        lines += ["", t(lang, "alert.triage.why", symbol=items[0]["symbol"], why=tr(lang, why))]
    lines.append(t(lang, "alert.triage.order"))
    return "\n".join(lines)


def sizing_line(check: dict, lang: str = EN) -> Optional[str]:
    if check.get("status") != "oversized":
        return None
    return t(lang, "alert.sizing.line", reasons=join(lang, check.get("reasons", []), "; "), size=f"{check['suggested_size']:g}",
             notional=_usd(check.get("suggested_notional"), lang), cut=f"{check['reduce_pct']:.0f}")


def exit_label(lang: str, kind: Optional[str]) -> str:
    key = f"risk.exit.{str(kind).replace(' ', '_')}"
    return t(lang, key) if has(key) else str(kind)


def format_sizing_message(checks: list[dict], demo: bool = False, lang: str = EN) -> str:
    """From `SizingCheck.to_dict()` of the oversized positions."""
    first = checks[0]
    lines = [
        t(lang, "alert.sizing.head") + demo_tag(lang, demo),
        "",
        t(lang, "alert.sizing.limits", equity=_usd(first["equity"], lang), risk=f"{first['max_risk_pct']:g}",
          margin=f"{first['max_margin_pct']:g}"),
    ]
    for c in checks:
        risk = t(lang, "common.na") if c.get("risk_pct") is None else f"{c['risk_pct']:.1f}%"
        lines.append(t(lang, "alert.sizing.row", symbol=c["symbol"], side=side_upper(lang, c["side"]), size=f"{c['size']:g}",
                       notional=_usd(c["notional"], lang), x=f"{c['notional_x_equity']:.1f}", loss=_usd(c.get("loss_at_exit_usd"), lang),
                       risk=risk, exit=exit_label(lang, c.get("exit_kind")),
                       margin=f"{c['margin_pct']:.0f}", suggested=f"{c['suggested_size']:g}",
                       suggested_notional=_usd(c.get("suggested_notional"), lang)))
    lines.append(t(lang, "alert.sizing.change"))
    return "\n".join(lines)


def stress_lines(stress: dict, lang: str = EN) -> list[str]:
    """Shock table and account-level liquidation move from `AccountStress.to_dict()`."""
    cells = []
    for s in stress["shocks"]:
        if s["market_move_pct"] < 0:
            result = t(lang, "alert.stress.liquidated") if s["liquidated"] else t(lang, "alert.stress.left", amount=_usd(s["margin_left"], lang))
            cells.append(f"{s['market_move_pct']:+.0f}%: {result}")
    lines = [t(lang, "alert.stress.together", cells=" · ".join(cells))]
    down, uni = stress.get("liq_move_down_pct"), stress.get("uniform_liq_move_down_pct")
    if down is not None:
        lines.append(t(lang, "alert.stress.account", down=f"{down:.1f}")
                     + (t(lang, "alert.stress.uniform", uni=f"{uni:.1f}") if uni is not None else ""))
    return lines


def comparison_line(c: dict, lang: str = EN) -> Optional[str]:
    """Platform's displayed liq next to LiqGuard's correlated liq, gap highlighted."""
    if c.get("correlated_price") is None or c.get("platform_price") is None:
        return None
    gap = c.get("gap_pct")
    flag = t(lang, "alert.compare.closer", gap=f"{gap:.1f}") if gap is not None and gap >= 1 else ""
    return t(lang, "alert.compare.line", symbol=c["symbol"], platform=format_price(c["platform_price"], lang),
             platform_dist=f"{c['platform_distance_pct']:.1f}", correlated=format_price(c["correlated_price"], lang),
             correlated_dist=f"{c['correlated_distance_pct']:.1f}") + flag


def format_correlation_message(stress: Optional[dict], concentration: dict, comparisons: list[dict],
                               severity: Optional[str] = None, demo: bool = False, lang: str = EN) -> str:
    icon = SEVERITY_ICON.get(severity or "", "🧲")
    lines = [t(lang, "alert.stress.head", icon=icon) + demo_tag(lang, demo), ""]
    if concentration.get("message"):
        lines += [f"🧲 {tr(lang, concentration['message'])}", ""]
    if stress:
        lines.append(t(lang, "alert.stress.account_head", venue=venue_name(lang, stress["venue"]), n=len(stress["symbols"])))
        lines += stress_lines(stress, lang)
    shown = [line for line in (comparison_line(c, lang) for c in comparisons) if line]
    if shown:
        lines += ["", t(lang, "alert.compare.head")] + [f"• {line}" for line in shown]
        lines.append(t(lang, "alert.compare.note"))
    return "\n".join(lines)
