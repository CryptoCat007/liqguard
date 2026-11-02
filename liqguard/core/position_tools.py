"""
The four per-position quick actions (rescue / wick stress / stop check /
funding impact) as reports in the reader's language. Pure: everything comes
in as plain data, so the Telegram buttons and the web dashboard buttons show
exactly the same thing.
"""

from __future__ import annotations

from typing import Optional

from liqguard.i18n import EN, Translator
from liqguard.models import Position
from liqguard.risk import RiskContext, project_funding

TOOLS = ("rescue", "wick", "stop", "funding")


def tool_label(T: Translator, tool: str) -> str:
    return T(f"tools.name.{tool}")


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


def side(T: Translator, value: str) -> str:
    return T(f"common.side.{str(value).lower()}")


def _option_lines(T: Translator, o: dict, recommended: str) -> list[str]:
    star = T("tools.rescue.recommended") if o["kind"] == recommended else ""
    params, costs = o.get("params", {}), o["costs"]
    if o["kind"] == "add_margin":
        what = T("tools.rescue.what_add", amount=money(params.get("amount_usd")))
    elif o["kind"] == "reduce":
        what = T("tools.rescue.what_reduce", pct=params.get("reduce_pct", 0))
    else:
        what = T("tools.rescue.what_hedge", venue=str(params.get("hedge_venue", "")).title(), size=params.get("hedge_size", 0))
    after = T("tools.rescue.no_liq") if o.get("no_liquidation") else T("tools.rescue.after", dist=pct(o.get("new_distance_pct")))
    if not o.get("protects_liquidation"):
        after += T("tools.rescue.unprotected")
    kind = o["kind"]
    return [
        T(f"tools.option.{kind}") + star,
        f"   {what} · {after}",
        "   " + T("tools.rescue.cost", total=money(costs["total_cost_usd"]), fees=money(costs["fees_usd"]),
                  realized=money(costs["realized_loss_usd"]), capital=money(costs["capital_required_usd"])),
    ]


def rescue_lines(T: Translator, plan: Optional[dict]) -> list[str]:
    if plan is None:
        return [T("tools.rescue.missing")]
    cur = plan["current"]
    lines = [T("tools.rescue.current", dist=pct(cur["distance_pct"]), price=price(cur["price"])),
             T("tools.rescue.target", target=plan["target_distance_pct"]), ""]
    if plan.get("already_safe"):
        return lines + [T("tools.rescue.already_safe")]
    for o in plan["options"]:
        lines += _option_lines(T, o, plan["recommended"]) + [""]
    return lines + [T("tools.rescue.footer")]


def wick_lines(T: Translator, info: dict) -> list[str]:
    wicks = info.get("wicks") or {}
    lines = [T("tools.wick.current", dist=pct(info.get("distance_pct")))]
    exposures = [e for e in wicks.get("exposures", []) if e.get("max_pct") is not None]
    if not exposures:
        lines.append(T("tools.wick.no_data"))
    for e in exposures:
        away = T("tools.wick.inside") if not e.get("max_wicks_away") else T("tools.wick.away", n=e["max_wicks_away"])
        lines.append(T("tools.wick.line", interval=e["interval"], typical=e["typical_pct"], max=e["max_pct"],
                       days=e.get("window_days", 30), away=away))
    if wicks.get("one_max_wick_liquidates"):
        lines.append(T("tools.wick.one_wick"))
    elif exposures:
        lines.append(T("tools.wick.safe"))
    t = info.get("thresholds") or {}
    if t.get("adapted"):
        e = t["effective"]
        lines.append(T("tools.wick.adaptive", warning=e["warning"], critical=e["critical"], emergency=e["emergency"]))
    return lines


def stop_lines(T: Translator, stop: Optional[dict]) -> list[str]:
    if stop is None:
        return [T("tools.stop.unavailable")]
    lines = [T(f"tools.stop.status.{stop['status']}")]
    if stop.get("stop_price") is not None:
        lines.append(T("tools.stop.price", price=price(stop["stop_price"])))
    if stop.get("gap_pct") is not None:
        lines.append(T("tools.stop.gap", gap=pct(stop["gap_pct"], 2), required=pct(stop["required_gap_pct"], 2)))
    lines.append(T("tools.stop.liq", price=price(stop.get("liquidation_price"))))
    if stop.get("loss_if_liquidated_usd"):
        lines.append(T("tools.stop.loss", loss=money(stop["loss_if_liquidated_usd"])))
    move = next((f for f in stop.get("fixes", []) if f["kind"] == "move_stop"), None)
    if stop.get("needs_warning") and move:
        lines.append(T("tools.stop.suggest_move", price=price(move["stop_price"])))
    elif stop["status"] == "no_stop":
        lines.append(T("tools.stop.suggest_set"))
    return lines


def funding_lines(T: Translator, position: Position, context: RiskContext) -> Optional[list[str]]:
    """Rate, next settlement payment and liquidation drift at the current rate, or None without funding data."""
    if not context.computable(position):
        return None
    proj = project_funding(position, context.account_for(position), context.funding_for, context.now)
    if proj is None:
        return None
    verb = T("tools.funding.pay") if proj.next_payment_usd > 0 else T("tools.funding.receive")
    lines = [
        T("tools.funding.rate", rate=proj.info.rate * 100, hours=proj.info.interval_hours),
        T("tools.funding.next", minutes=proj.minutes_to_settlement, verb=verb, amount=money(abs(proj.next_payment_usd))),
    ]
    drift = T("common.list_sep").join(
        T("tools.funding.drift_item", hours=d.horizon_hours, dist=pct(d.distance_pct)) if d.liquidation_price
        else T("tools.funding.drift_none", hours=d.horizon_hours)
        for d in proj.drifts
    )
    if drift:
        lines.append(T("tools.funding.drift", drift=drift))
    return lines


def close_fee_line(T: Translator, position: Position, context: RiskContext) -> str:
    fee = context.fees.taker_fee(position.venue, abs(position.size) * position.mark_price)
    return T("tools.funding.close_fee", rate=context.fees.taker_rate(position.venue) * 100, fee=money(fee))


def muse_line(T: Translator, note: dict) -> str:
    return f"✦ {T('alert.muse_mock' if note.get('mock') else 'alert.muse')}: {note['text']}"


def report(tool: str, position: Position, context: RiskContext, protection: dict,
           plan: Optional[dict] = None, demo: bool = False, lang: str = EN, muse: Optional[dict] = None) -> dict:
    """
    {tool, title, lines, muse} for one quick action on one position, in `lang`.
    `muse` is `MuseService.tool_note` output ({"text", "mock"}): the rescue
    recommendation or stop-loss fix in words, shown as the last line.
    """
    if tool not in TOOLS:
        raise ValueError(f"unknown tool {tool!r}")
    T = Translator(lang)
    tag = T("common.demo_data") if demo else ""
    title = T("tools.title", tool=tool_label(T, tool), symbol=position.symbol, side=side(T, position.side.value), demo=tag)
    if tool == "rescue":
        lines = rescue_lines(T, plan)
    elif tool == "wick":
        lines = wick_lines(T, protection)
    elif tool == "stop":
        lines = stop_lines(T, protection.get("stop"))
    else:
        lines = (funding_lines(T, position, context) or [T("tools.funding.no_data")]) + [close_fee_line(T, position, context)]
    if muse:
        lines = [*lines, "", muse_line(T, muse)]
    return {"tool": tool, "symbol": position.symbol, "venue": position.venue.value, "title": title, "lines": lines,
            "lang": T.lang, "muse": muse}
