"""🧾 Account & actions: status, testnet-only protective actions (confirm / cancel) and the kill switch."""

from __future__ import annotations

from liqguard.i18n import LANGUAGE_NAMES

from .. import keyboards as kb
from ..screen import Request, Screen
from ..texts import money


async def account(req: Request, toast: str = None) -> Screen:
    s, T = req.services, req.T
    user = await s.user(req.user_id)
    actions = s.actions
    stats = actions.get_user_stats(user.telegram_id)
    active = [b for b in user.bindings if b.is_active]
    t = user.threshold
    lines = [
        T("account.title"),
        "",
        T("account.id", id=user.telegram_id),
        T("account.language", name=LANGUAGE_NAMES.get(T.lang, T.lang)),
        T("account.demo_on" if user.is_demo_mode else "account.demo_off"),
        T("account.bound", names=T.join(sorted({b.venue.title() + (T("common.testnet") if b.is_testnet else "") for b in active})))
        if active else T("account.unbound"),
        T("account.tiers", warning=t.warning_distance_pct, critical=t.critical_distance_pct, emergency=t.emergency_distance_pct),
        "",
        T("account.protect_on" if actions.is_enabled() else "account.protect_off"),
        T("account.kill_on" if stats["kill_switch_active"] else "account.kill_off"),
        T("account.daily", total=money(stats["daily_total_usd"]), limit=money(stats["daily_limit_usd"])),
        "",
    ]
    pending = actions.get_pending_actions(user.telegram_id)
    if pending:
        lines.append(T("account.pending_title"))
        for a in pending:
            adv = a.advice
            lines.append(T("account.pending_row", id=a.action_id[-4:].upper(), kind=T(f"audit.action_type.{adv.action_type.value}"),
                           symbol=adv.symbol, amount=money(adv.margin_to_add_usd or 0), status=T(f"audit.status.{a.status.value}")))
    else:
        lines.append(T("account.pending_none"))
    buttons = [(a.action_id, a.action_id[-4:].upper()) for a in pending]
    return Screen("\n".join(lines), kb.account(T, req.sign, buttons, stats["kill_switch_active"]), toast=toast)


async def kill_switch(req: Request) -> Screen:
    (want,) = req.args
    actions = req.services.actions
    if want == "on":
        if not actions.is_kill_switch_active(req.user_id):
            actions.activate_kill_switch(req.user_id)
        toast = req.T("account.kill_on_toast")
    else:
        if actions.is_kill_switch_active(req.user_id):
            actions.deactivate_kill_switch(req.user_id)
        toast = req.T("account.kill_off_toast")
    return await account(req, toast=toast)


async def confirm(req: Request) -> Screen:
    (action_id,) = req.args
    actions = req.services.actions
    ok, reason = actions.confirm_action(action_id, req.user_id)
    if ok:
        ok, reason = await actions.execute_action(action_id, req.user_id)
    return await account(req, toast=req.T("account.done_toast") if ok else req.T("account.failed_toast", reason=reason))


async def cancel(req: Request) -> Screen:
    (action_id,) = req.args
    ok, reason = req.services.actions.cancel_action(action_id, req.user_id)
    return await account(req, toast=req.T("account.cancelled_toast") if ok else req.T("account.failed_toast", reason=reason))


HANDLERS = {"ac": account, "ak": kill_switch, "ay": confirm, "an": cancel}
