"""📊 Risk overview (Muse's summary, handling order, protection flags) and 🛡 Stop-loss check across every position."""

from __future__ import annotations

from typing import Optional

from liqguard.alerts import recommendation_line
from liqguard.risk import position_protection

from .. import keyboards as kb
from ..screen import Request, Screen
from ..texts import book_label, money, pct, severity, side

LEVEL_EMOJI = {"safe": "✅", "low": "🟢", "moderate": "🟡", "high": "🟠", "critical": "🔴"}
STOP_ATTENTION = ("no_stop", "beyond_liquidation", "too_close", "partial")


async def _book(req: Request):
    user = await req.services.user(req.user_id)
    snapshot = await req.services.snapshot(user)
    if not snapshot.label:
        return user, snapshot, Screen(req.T("positions.no_account"), kb.no_account(req.T, req.sign))
    return user, snapshot, None


async def overview(req: Request) -> Screen:
    s, T = req.services, req.T
    user, snapshot, empty = await _book(req)
    if empty is not None:
        return empty
    if not snapshot.positions:
        return Screen(f"{book_label(T, snapshot.label)}\n\n{T('positions.none')}", kb.refresh_back(T, req.sign, "ro"))
    book = s.muse.portfolio(user, snapshot, T.lang)
    ov, insights = book.overview, book.insights
    infos = [(p, position_protection(p, snapshot.context, user.threshold).to_dict()) for p in snapshot.positions]

    body = [
        T("overview.level", emoji=LEVEL_EMOJI.get(ov.overall_risk_level.value, "❓"),
          level=T(f"overview.levels.{ov.overall_risk_level.value}"), n=ov.total_positions, at_risk=len(insights.at_risk)),
    ]
    if ov.riskiest_position is not None:
        r = ov.riskiest_position
        body.append(T("overview.closest", symbol=r.symbol, side=side(T, r.side), dist=pct(r.distance_to_liquidation_pct)))
    if ov.total_margin_needed_for_safety > 0:
        body.append(T("overview.margin", target=user.threshold.safety_target_pct, amount=money(ov.total_margin_needed_for_safety)))
    body.append(T("overview.pnl", pnl=money(ov.total_unrealized_pnl, signed=True)))

    body += ["", T("overview.order")]
    if insights.at_risk:
        body += [T("triage.row", rank=t.rank, symbol=t.symbol, side=side(T, t.side), severity=severity(T, t.severity),
                   dist=pct(t.distance_pct), moves="") for t in insights.at_risk[:5]]
    else:
        body.append(T("overview.all_safe"))

    stops = [i.get("stop") for _, i in infos]
    flagged = [p.symbol for (p, i) in infos if (i.get("stop") or {}).get("status") in STOP_ATTENTION]
    body.append("")
    if flagged:
        body.append(T("overview.stops_bad", symbols=T.join(flagged)))
    elif all(st is None for st in stops):
        body.append(T("overview.stops_unknown"))
    else:
        body.append(T("overview.stops_ok"))
    one_wick = [p.symbol for p, i in infos if (i.get("wicks") or {}).get("one_max_wick_liquidates")]
    if one_wick:
        body.append(T("overview.one_wick", symbols=T.join(one_wick)))
    adl = [p.symbol for p, i in infos if (i.get("adl") or {}).get("warn")]
    if adl:
        body.append(T("overview.adl", symbols=T.join(adl)))
    body += ["", T("overview.hint")]

    def text(summary: dict) -> str:
        head = recommendation_line(T.lang, summary["headline"], not summary.get("mock", True))
        return "\n".join([T("overview.title", book=book_label(T, snapshot.label)), "", head, "", *body])

    first: Optional[tuple[str, str]] = None
    focus = insights.focus
    if focus and insights.at_risk:
        pos = next((p for p in snapshot.positions if p.symbol == focus["symbol"] and p.venue.value == focus["venue"]), None)
        if pos is not None:
            first = (s.source_of(snapshot, pos), pos.symbol)

    later = None
    if book.summary.pending is not None:
        pending = book.summary.pending

        async def later() -> Optional[str]:
            live = await pending
            return text(live) if live else None

    return Screen(text(book.summary.result), kb.risk_overview(T, req.sign, first), later=later)


async def stop_check_list(req: Request) -> Screen:
    s, T = req.services, req.T
    user, snapshot, empty = await _book(req)
    if empty is not None:
        return empty
    if not snapshot.positions:
        return Screen(f"{book_label(T, snapshot.label)}\n\n{T('positions.none')}", kb.refresh_back(T, req.sign, "sc"))
    rows, buttons = [], []
    for p, _, info in s.assessed(user, snapshot)[: kb.MAX_POSITIONS]:
        stop = info.get("stop")
        status = T(f"tools.stop.status.{stop['status']}") if stop else T("stopcheck.unknown")
        rows.append(T("stopcheck.row", symbol=p.symbol, side=side(T, p.side.value), status=status))
        buttons.append((s.source_of(snapshot, p), p.symbol, T("stopcheck.button", symbol=p.symbol, side=side(T, p.side.value))))
    text = "\n".join([T("stopcheck.title", book=book_label(T, snapshot.label)), "", *rows, "", T("stopcheck.hint")])
    return Screen(text, kb.stop_list(T, req.sign, buttons))


HANDLERS = {"ro": overview, "sc": stop_check_list}
