"""💎 LiqGuard Pro: PayPal sandbox subscription, manage, Rescue Pass, Muse agent checkout."""

from __future__ import annotations

from .. import keyboards as kb
from ..screen import Request, Screen


async def upgrade(req: Request) -> Screen:
    T = req.T
    if req.chat_type != "private":
        return Screen(T("pro.private_only"), kb.back_only(T, req.sign), toast=T("common.use_private"), alert=True)
    if not req.services.paypal_enabled():
        return Screen(T("pro.disabled"), kb.back_only(T, req.sign))

    status = await req.services.billing_status(req.user_id)
    if status.is_pro:
        status_key = {
            "ACTIVE": "pro.status_active",
            "CANCELLED": "pro.status_cancelled",
            "SUSPENDED": "pro.status_suspended",
        }.get(status.subscription_status or "ACTIVE", "pro.status_active")
        text = T(
            "pro.screen_pro",
            plan=T("pro.billing.plan", plan=status.plan_id or T("pro.tier_pro")),
            status=T(status_key),
            subscription=T("pro.billing.subscription_id", id=status.subscription_id or T("common.na")),
            passes=T("pro.rescue_pass_remaining", n=status.rescue_passes_remaining),
            muse_unlimited=T("pro.limits.muse_unlimited"),
        )
        return Screen(text, kb.pro_upgrade(T, req.sign, manage=True))

    allowed, used, limit = (True, 0, -1)
    entitlements = req.services.ctx.entitlements
    if entitlements is not None:
        allowed, used, limit = await entitlements.check_muse_limit(req.user_id)
    limits = T("pro.limits.muse_unlimited") if limit < 0 else T("pro.limits.muse_daily", used=used, limit=limit)
    text = T(
        "pro.screen_free",
        hint=T("pro.upgrade_hint"),
        polling=T("pro.features.polling"),
        accounts=T("pro.features.accounts"),
        muse=T("pro.features.muse"),
        priority=T("pro.features.priority"),
        limits=limits,
    )
    url = None
    extra = ""
    if req.services.ctx.settings.paypal_plan_id:
        url = await req.services.create_pro_checkout(req.user_id)
        extra = "\n\n" + (T("pro.checkout_ready") if url else T("pro.checkout"))
    else:
        extra = "\n\n" + T("pro.no_plan")
    return Screen(text + extra, kb.pro_upgrade(T, req.sign, url), new_message=bool(url))


async def manage(req: Request) -> Screen:
    T = req.T
    if req.chat_type != "private":
        return Screen(T("pro.private_only"), kb.back_only(T, req.sign), toast=T("common.use_private"), alert=True)
    if not req.services.paypal_enabled():
        return Screen(T("pro.disabled"), kb.back_only(T, req.sign))

    status = await req.services.billing_status(req.user_id)
    if status.is_pro and status.subscription_id:
        ok = await req.services.cancel_pro(req.user_id)
        toast = T("pro.cancelled_ok") if ok else T("pro.cancel_failed")
        return Screen(T("pro.billing.title") + "\n\n" + toast, kb.pro_manage(T, req.sign), toast=toast)

    url = None
    if req.services.ctx.settings.paypal_plan_id:
        url = await req.services.create_pro_checkout(req.user_id)
    text = T("pro.upgrade_title") + "\n\n" + T("pro.upgrade_hint")
    if url:
        text += "\n\n" + T("pro.checkout_ready")
    return Screen(text, kb.pro_manage(T, req.sign, url), new_message=bool(url))


async def rescue_pass_screen(req: Request) -> Screen:
    T = req.T
    if req.chat_type != "private":
        return Screen(T("pro.private_only"), kb.back_only(T, req.sign), toast=T("common.use_private"), alert=True)
    if not req.services.paypal_enabled():
        return Screen(T("pro.disabled"), kb.back_only(T, req.sign))

    url = await req.services.create_rescue_pass_checkout(req.user_id)
    text = T("pro.rescue_pass_title") + "\n\n" + T("pro.rescue_pass_hint")
    if url:
        text += "\n\n" + T("pro.checkout_ready")
    return Screen(text, kb.rescue_pass(T, req.sign, url), new_message=bool(url))


async def agent_checkout_screen(req: Request) -> Screen:
    """Muse (or rules) picks a PayPal Agent Toolkit tool and returns an approve URL button."""
    T = req.T
    if req.chat_type != "private":
        return Screen(T("pro.private_only"), kb.back_only(T, req.sign), toast=T("common.use_private"), alert=True)
    if not req.services.paypal_enabled():
        return Screen(T("pro.disabled"), kb.back_only(T, req.sign))

    kind = req.args[0] if req.args else "auto"
    product = None if kind == "auto" else kind
    text = (req.extra.get("text") or "").strip()
    if not text:
        text = {
            "rescue": "get me a Rescue Pass",
            "pro": "upgrade to Pro",
            "auto": "upgrade to Pro",
        }.get(kind, "upgrade to Pro")
    checkout = await req.services.agent_checkout(req.user_id, text, T.lang, product=product,
                                                 default_product="pro" if kind == "auto" else None)
    if checkout.status == "none":
        return Screen(T("pro.agent.none"), kb.agent_checkout(T, req.sign))
    if checkout.status != "created" or not checkout.approve_url:
        body = T("pro.agent.error")
        if checkout.message:
            body += "\n\n" + checkout.message
        return Screen(body, kb.agent_checkout(T, req.sign))
    ready = T("pro.agent.rescue_ready") if checkout.product == "rescue" else T("pro.agent.pro_ready")
    source = T("pro.agent.source_muse") if checkout.source == "muse" else T("pro.agent.source_rules")
    text_body = T("pro.agent.title") + "\n\n" + ready + "\n" + source
    return Screen(text_body, kb.agent_checkout(T, req.sign, checkout.approve_url, checkout.product),
                  new_message=True)


HANDLERS = {"pu": upgrade, "pm": manage, "rp": rescue_pass_screen, "px": agent_checkout_screen}
