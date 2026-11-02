"""
Muse's structured explanation of a rescue plan.

The plan (`risk.RescuePlan`) holds every number. Muse may only phrase the
trade-offs: a live answer is rejected (and the deterministic mock used
instead) if it recommends an option the plan does not contain or quotes a
dollar amount that is not in the plan.
"""

from __future__ import annotations

import re
from typing import Optional

from liqguard.i18n import EN, ZH, is_zh, tr
from liqguard.risk.rescue import ADD_MARGIN, HEDGE, OPTION_ORDER, REDUCE, RescueOption, RescuePlan

RESCUE_EXPLANATION_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One sentence naming the recommended option"},
        "recommended_option": {"type": "string", "enum": list(OPTION_ORDER)},
        "trade_offs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "option": {"type": "string", "enum": list(OPTION_ORDER)},
                    "summary": {"type": "string"},
                    "pros": {"type": "array", "items": {"type": "string"}},
                    "cons": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["option", "summary", "pros", "cons"],
            },
        },
        "caveat": {"type": "string"},
    },
    "required": ["headline", "recommended_option", "trade_offs", "caveat"],
}

_DOLLARS = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)")


def _usd(x: float, dp: int = 2) -> str:
    return f"${x:,.{dp}f}"


def _dist(o: RescueOption, zh: bool = False) -> str:
    if o.new_liquidation_price is None:
        return "無清算價" if zh else "no liquidation"
    return f"{o.new_distance_pct:.1f}%"


def _summary(o: RescueOption, zh: bool) -> str:
    liq = _usd(o.new_liquidation_price) if o.new_liquidation_price else ("無" if zh else "none")
    if zh:
        return (
            f"{tr(ZH, o.title)}：新清算價 {liq}（距離 {_dist(o, zh)}）；手續費 {_usd(o.fees_usd)}、"
            f"已實現虧損 {_usd(o.realized_loss_usd)}、24 小時資金費 {_usd(o.funding_24h_usd)}（按當前費率）。"
        )
    return (
        f"{tr(EN, o.title)}: new liquidation {liq} ({_dist(o)} away); fees {_usd(o.fees_usd)}, "
        f"realized loss {_usd(o.realized_loss_usd)}, 24h funding {_usd(o.funding_24h_usd)} at current rate."
    )


def _pros_cons(o: RescueOption, zh: bool) -> tuple[list[str], list[str]]:
    pros, cons = [], []
    if o.kind == ADD_MARGIN:
        pros = ["無手續費、無已實現虧損", "保留全部倉位"] if zh else ["No fees and no realized loss", "Keeps the full position"]
        cons = [f"需投入 {_usd(o.capital_required_usd)} 新資金" if zh else f"Commits {_usd(o.capital_required_usd)} of new capital",
                "價格繼續不利時，追加的保證金同樣承擔風險" if zh else "The added margin is at risk too if the price keeps moving"]
    elif o.kind == REDUCE:
        pros = ["不需要新資金" if zh else "Needs no new capital",
                "降低曝險與資金費" if zh else "Cuts exposure and the funding you pay"]
        if o.realized_loss_usd > 0:
            cons.append(f"鎖定 {_usd(o.realized_loss_usd)} 已實現虧損" if zh else f"Locks in a {_usd(o.realized_loss_usd)} realized loss")
        cons.append(f"{_usd(o.fees_usd)} 吃單手續費" if zh else f"{_usd(o.fees_usd)} in taker fees")
        if not o.reaches_target:
            cons.append("即使全部平倉也無法達到目標" if zh else "Even a full close does not reach the target")
    elif o.kind == HEDGE:
        if o.protects_liquidation:
            pros = ["凍結對沖部分的盈虧" if zh else "Freezes PnL on the hedged part",
                    "目前不實現虧損" if zh else "No loss realized now"]
            cons = [f"{_usd(o.fees_usd)} 開倉及平倉手續費" if zh else f"{_usd(o.fees_usd)} in fees to open and later close",
                    "需要 Binance 雙向持倉模式" if zh else "Requires Binance Hedge Mode"]
        else:
            pros = ["保護盈虧不受進一步下跌影響" if zh else "Protects PnL against a further move"]
            cons = ["不會移動此倉位的清算價" if zh else "Does not move this position's liquidation price",
                    f"需在另一交易所投入 {_usd(o.capital_required_usd)}" if zh else f"Needs {_usd(o.capital_required_usd)} on another venue"]
    return pros, cons


def mock_rescue_explanation(plan: RescuePlan, language: str = "en") -> dict:
    zh = is_zh(language)
    best = plan.option(plan.recommended)
    target = plan.target_distance_pct
    if plan.already_safe:
        dist = plan.current.distance_pct
        if zh:
            headline = (f"{plan.symbol} 沒有清算價，無需救援。" if dist is None else
                        f"{plan.symbol} 距清算 {dist:.1f}%，已達 {target:.0f}% 目標，目前無需救援。")
        else:
            headline = (f"{plan.symbol} has no liquidation price; no rescue needed." if dist is None else
                        f"{plan.symbol} is {dist:.1f}% from liquidation, already beyond the {target:.0f}% target; no rescue needed now.")
    elif best is None:
        headline = "沒有可用的方案。" if zh else "No rescue option is available."
    elif zh:
        headline = (
            f"{plan.symbol}：建議「{tr(ZH, best.title)}」，以 {_usd(best.total_cost_usd)} 的成本"
            f"{'恢復 ' + format(target, '.0f') + '% 緩衝' if best.reaches_target else '擴大緩衝'}。"
        )
    else:
        goal = f"back to a {target:.0f}% buffer" if best.reaches_target else "to widen the buffer"
        headline = f"{plan.symbol}: cheapest way {goal} — {tr(EN, best.title)} ({_usd(best.total_cost_usd)} incl. fees and 24h funding)."
    trade_offs = []
    for o in plan.options:
        pros, cons = _pros_cons(o, zh)
        trade_offs.append({"option": o.kind, "summary": _summary(o, zh), "pros": pros, "cons": cons})
    caveat = (
        "資金費按當前費率推算，而費率每次結算都會變動。所有數字為估算，未經你確認不會執行任何操作。"
        if zh else
        "Funding is projected at the current rate, which changes every settlement. "
        "All figures are estimates; nothing is executed without your confirmation."
    )
    return {"headline": headline, "recommended_option": plan.recommended, "trade_offs": trade_offs, "caveat": caveat, "mock": True}


def plan_amounts(plan: RescuePlan) -> set[float]:
    """Every dollar figure the explanation may quote, at the rounding a reader would use."""
    values = [plan.mark_price, plan.current.wallet_balance, plan.current.margin_buffer]
    if plan.current.price:
        values.append(plan.current.price)
    for o in plan.options:
        values += [o.fees_usd, o.funding_24h_usd, abs(o.funding_24h_usd), o.realized_loss_usd,
                   o.capital_required_usd, o.total_cost_usd, abs(o.total_cost_usd)]
        if o.new_liquidation_price:
            values.append(o.new_liquidation_price)
        values += [v for v in o.params.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    out = set()
    for v in values:
        for dp in (0, 1, 2):
            out.add(round(v, dp))
    return out


def validate_rescue_explanation(data: dict, plan: RescuePlan) -> Optional[str]:
    """None if `data` is acceptable, else the reason it was rejected."""
    kinds = {o.kind for o in plan.options}
    if data.get("recommended_option") not in kinds:
        return "recommended_option not in plan"
    if not isinstance(data.get("headline"), str) or not data["headline"].strip():
        return "missing headline"
    trade_offs = data.get("trade_offs")
    if not isinstance(trade_offs, list) or not trade_offs:
        return "missing trade_offs"
    for t in trade_offs:
        if not isinstance(t, dict) or t.get("option") not in kinds:
            return "trade-off for an unknown option"
    allowed = plan_amounts(plan)
    text = " ".join([data["headline"], str(data.get("caveat", ""))] + [
        " ".join([str(t.get("summary", ""))] + [str(x) for x in t.get("pros", []) + t.get("cons", [])])
        for t in trade_offs
    ])
    for match in _DOLLARS.findall(text):
        value = float(match.replace(",", ""))
        if not any(abs(value - a) <= 0.011 for a in allowed):
            return f"amount ${match} not in plan"
    return None
