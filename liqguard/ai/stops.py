"""
Muse's explanation of a stop-loss check (`risk.StopCheck`).

The check holds every number: the liquidation price, the stop, the gap and
each fix (safe stop price, margin to add, size reduction). Muse only picks
one of those fixes and says why. A live answer is rejected (and the
deterministic mock used) if it suggests a fix the check doesn't contain or
quotes a dollar amount that isn't in it.
"""

from __future__ import annotations

from typing import Optional

from liqguard.i18n import is_zh
from liqguard.risk.stops import BEYOND_LIQUIDATION, NO_STOP, TOO_CLOSE, StopCheck, StopFix

from . import amounts, figures

FIX_KINDS = ("move_stop", "add_margin", "reduce")

STOP_EXPLANATION_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One sentence: what is wrong with the stop-loss"},
        "suggested_fix": {"type": "string", "enum": list(FIX_KINDS) + ["none"]},
        "explanation": {"type": "string", "description": "Why this fix, quoting only numbers from the input"},
        "caveat": {"type": "string"},
    },
    "required": ["headline", "suggested_fix", "explanation", "caveat"],
}

def _usd(x: Optional[float], dp: int = 2) -> str:
    return "n/a" if x is None else f"${x:,.{dp}f}"


def _preferred(check: StopCheck) -> Optional[StopFix]:
    """Keeping the trader's own stop is preferred when a size fix exists; otherwise move the stop."""
    if check.status == NO_STOP:
        return check.fix("move_stop")
    return check.fix("reduce") or check.fix("move_stop") or check.fix("add_margin")


def mock_stop_explanation(check: StopCheck, language: str = "en") -> dict:
    zh = is_zh(language)
    fix = _preferred(check)
    liq = _usd(check.liquidation_price)
    if check.status == NO_STOP:
        headline = (f"{check.symbol} 沒有停損單；價格觸及 {liq} 會被強平。" if zh else
                    f"{check.symbol} has no stop-loss; at {liq} the exchange liquidates it.")
    elif check.status == BEYOND_LIQUIDATION:
        headline = (f"{check.symbol} 的停損 {_usd(check.stop_price)} 在清算價 {liq} 之後，永遠不會觸發。" if zh else
                    f"{check.symbol}'s stop at {_usd(check.stop_price)} sits beyond the liquidation price {liq}: "
                    f"the exchange liquidates first and the stop never fires.")
    elif check.status == TOO_CLOSE:
        headline = (f"{check.symbol} 的停損 {_usd(check.stop_price)} 離清算價 {liq} 只有 {check.gap_pct:.2f}%。" if zh else
                    f"{check.symbol}'s stop at {_usd(check.stop_price)} is only {check.gap_pct:.2f}% in front of "
                    f"liquidation at {liq}; a stop-market fill can slip past it in a wick.")
    else:
        headline = f"{check.symbol} 的停損設定沒有問題。" if zh else f"{check.symbol}'s stop-loss is fine."

    if fix is None:
        explanation = ("目前沒有可計算的修正。" if zh else "No computed fix is available.")
        kind = "none"
    elif fix.kind == "move_stop":
        explanation = (
            f"把停損設在 {_usd(fix.stop_price)}（清算前保留 {check.required_gap_pct:.2f}% 緩衝，"
            f"至少一根典型 5 分鐘影線）；觸發時已實現損益約 {_usd(fix.pnl_at_stop_usd)}（含手續費）。" if zh else
            f"Place the stop at {_usd(fix.stop_price)}: that leaves {check.required_gap_pct:.2f}% before liquidation "
            f"(at least a typical 5m wick), and closing there realizes {_usd(fix.pnl_at_stop_usd)} including fees."
        )
        kind = fix.kind
    elif fix.kind == "reduce":
        explanation = (
            f"保留停損 {_usd(fix.stop_price)}，把倉位減少 {fix.reduce_pct:.1f}%（剩 {fix.new_size:.4f}），"
            f"清算價移到 {_usd(fix.new_liquidation_price)}，停損會先觸發；手續費 {_usd(fix.fees_usd)}。" if zh else
            f"Keep your stop at {_usd(fix.stop_price)} and cut the position {fix.reduce_pct:.1f}% "
            f"(to {fix.new_size:.4f}): liquidation moves to {_usd(fix.new_liquidation_price)}, so the stop fires first. "
            f"Fees {_usd(fix.fees_usd)}."
        )
        kind = fix.kind
    else:
        explanation = (
            f"保留停損 {_usd(fix.stop_price)}，追加 {_usd(fix.add_margin_usd)} 保證金，清算價移到 "
            f"{_usd(fix.new_liquidation_price)}。" if zh else
            f"Keep your stop at {_usd(fix.stop_price)} and add {_usd(fix.add_margin_usd)} margin: liquidation moves to "
            f"{_usd(fix.new_liquidation_price)}, behind the stop."
        )
        kind = fix.kind
    caveat = ("數字以目前標記價與當前清算價計算；價格與資金費變動會改變它們。未經你確認不會執行任何操作。" if zh else
              "Computed from the current mark and liquidation price, which move with price and funding. "
              "LiqGuard never places or edits orders.")
    return {"headline": headline, "suggested_fix": kind, "explanation": explanation, "caveat": caveat, "mock": True}


def stop_amounts(check: StopCheck) -> set[float]:
    """Every dollar figure the explanation may quote: the check's own, and the sum of two figures of one fix."""
    head = [check.mark_price, check.liquidation_price, check.stop_price, check.pnl_at_stop_usd, check.loss_if_liquidated_usd]
    return amounts.with_sums([head, *([f.stop_price, f.pnl_at_stop_usd, f.add_margin_usd, f.new_liquidation_price, f.fees_usd]
                                      for f in check.fixes)])


def stop_figures(check: StopCheck) -> dict:
    """The check as Muse should quote it: every amount already written for the reader."""
    def fix(f: StopFix) -> dict:
        out = {
            "fix": f.kind,
            "stop_price": figures.price(f.stop_price),
            "pnl_if_stop_fills": figures.signed_usd(f.pnl_at_stop_usd),
            "add_margin": figures.usd(f.add_margin_usd, cents=True) if f.add_margin_usd else None,
            "reduce_position_by": figures.pct(f.reduce_pct) if f.reduce_pct else None,
            "new_liquidation_price": figures.price(f.new_liquidation_price),
            "fees": figures.usd(f.fees_usd, cents=True) if f.fees_usd else None,
        }
        return {k: v for k, v in out.items() if v is not None}

    out = {
        "symbol": check.symbol,
        "side": check.side,
        "status": check.status,
        "mark_price": figures.price(check.mark_price),
        "liquidation_price": figures.price(check.liquidation_price),
        "stop_price": figures.price(check.stop_price) if check.stop_price is not None else "none",
        "gap_between_stop_and_liquidation": figures.pct(check.gap_pct, 2) if check.gap_pct is not None else None,
        "required_gap": figures.pct(check.required_gap_pct, 2),
        "loss_if_liquidated": figures.usd(check.loss_if_liquidated_usd, cents=True) if check.loss_if_liquidated_usd else None,
        "fixes": [fix(f) for f in check.fixes],
    }
    return {k: v for k, v in out.items() if v is not None}


def validate_stop_explanation(data: dict, check: StopCheck) -> Optional[str]:
    kinds = {f.kind for f in check.fixes} | {"none"}
    if data.get("suggested_fix") not in kinds:
        return "suggested_fix not in check"
    for key in ("headline", "explanation"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            return f"missing {key}"
    text = " ".join(str(data.get(k, "")) for k in ("headline", "explanation", "caveat"))
    shown = amounts.unlisted(text, stop_amounts(check))
    return f"amount {shown} not in check" if shown else None
