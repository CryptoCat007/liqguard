"""
Alert buttons: ✅ I'm awake and 😴 Snooze (🚑 Handle this first is the positions
feature's rescue sim, ⏹ Stop demo the demo feature's). On a live alert ✅ silences
that position until it materially worsens; on a demo alert either button mutes
the rest of that demo, so nothing more arrives from it.
"""

from __future__ import annotations

from .. import keyboards as kb
from ..screen import Request, Screen

SNOOZE_MINUTES = 30


async def acknowledge(req: Request) -> Screen:
    T = req.T
    result = await req.services.acknowledge(req.user_id, req.args[0])
    if result is None:
        return Screen(T("alerts.gone"), toast=T("alerts.gone"), edit=False, strip_keyboard=True)
    symbol, demo = result
    if demo:
        return Screen(T("alerts.demo_acked", symbol=symbol), kb.back_only(T, req.sign),
                      toast=T("alerts.demo_acked_toast"), new_message=True, strip_keyboard=True)
    return Screen(T("alerts.acked", symbol=symbol), kb.back_only(T, req.sign),
                  toast=T("alerts.acked_toast"), new_message=True, strip_keyboard=True)


async def snooze(req: Request) -> Screen:
    T = req.T
    result = await req.services.snooze(req.user_id, req.args[0], SNOOZE_MINUTES)
    if result is None:
        return Screen(T("alerts.gone"), toast=T("alerts.gone"), edit=False, strip_keyboard=True)
    symbol, demo = result
    if demo:
        return Screen(T("alerts.demo_quiet", symbol=symbol), kb.back_only(T, req.sign),
                      toast=T("alerts.demo_quiet_toast"), new_message=True, strip_keyboard=True)
    return Screen(T("alerts.snoozed", symbol=symbol, minutes=SNOOZE_MINUTES), kb.back_only(T, req.sign),
                  toast=T("alerts.snoozed_toast", minutes=SNOOZE_MINUTES), new_message=True, strip_keyboard=True)


HANDLERS = {"aa": acknowledge, "az": snooze}
