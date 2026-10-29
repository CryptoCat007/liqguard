"""🔔 Alerts: alert tiers, wake-up escalation interval/count and quiet hours (same rules as the dashboard)."""

from __future__ import annotations

from liqguard.core.user_settings import PRESETS, apply_setting, settings_dict
from liqguard.i18n import Translator

from .. import keyboards as kb
from ..screen import Request, Screen


def _text(T: Translator, current: dict, note: str = "") -> str:
    onoff = {True: T("common.on"), False: T("common.off")}
    return T(
        "settings.text",
        warning=current["warning"], critical=current["critical"], emergency=current["emergency"],
        escalation=onoff[current["escalation"]], interval=current["interval"], max=current["max"],
        quiet=current["quiet_label"], offset=current["quiet_utc_offset_hours"],
        adaptive=onoff[current["adaptive"]], stops=onoff[current["stops"]],
    ) + (f"\n\n{note}" if note else "") + "\n\n" + T("settings.tap")


async def show(req: Request) -> Screen:
    T = req.T
    user = await req.services.user(req.user_id)
    current = settings_dict(user.threshold, T.lang)
    return Screen(_text(T, current), kb.settings(T, req.sign, current))


async def pick(req: Request) -> Screen:
    key, T = req.args[0], req.T
    user = await req.services.user(req.user_id)
    current = settings_dict(user.threshold, T.lang)
    if key not in PRESETS:
        updated = apply_setting(user.threshold, key, "toggle")
        if updated is not None:
            await req.services.save_threshold(user, updated)
            current = settings_dict(updated, T.lang)
        return Screen(_text(T, current), kb.settings(T, req.sign, current), toast=T("settings.updated"))
    value = current[key] if key != "quiet" else (current["quiet"] or "off")
    text = f"🔔 {T(f'settings.label.{key}')}\n\n{T(f'settings.hint.{key}')}\n\n{T('settings.choose')}"
    return Screen(text, kb.setting_options(T, req.sign, key, PRESETS[key], value))


async def apply(req: Request) -> Screen:
    (key, value), T = req.args, req.T
    user = await req.services.user(req.user_id)
    updated = apply_setting(user.threshold, key, value)
    if updated is None:
        current = settings_dict(user.threshold, T.lang)
        return Screen(_text(T, current, T("settings.invalid")), kb.settings(T, req.sign, current),
                      toast=T("settings.invalid_toast"), alert=True)
    await req.services.save_threshold(user, updated)
    current = settings_dict(updated, T.lang)
    return Screen(_text(T, current, T("settings.saved_note")), kb.settings(T, req.sign, current), toast=T("settings.saved"))


HANDLERS = {"se": show, "sp": pick, "ss": apply}
