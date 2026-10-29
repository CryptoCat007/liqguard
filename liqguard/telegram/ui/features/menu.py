"""🏠 Main menu and ❓ Help."""

from __future__ import annotations

from liqguard.core.binding import security_note

from .. import keyboards as kb
from ..screen import Request, Screen


async def main_menu(req: Request) -> Screen:
    T = req.T
    user = await req.services.user(req.user_id)
    active = [b for b in user.bindings if b.is_active]
    if user.is_demo_mode:
        status = T("menu.status_demo")
    elif active:
        names = T.join(sorted({b.venue.title() + (T("common.testnet") if b.is_testnet else "") for b in active}))
        status = T("menu.status_bound", names=names)
    else:
        status = T("menu.status_unbound")
    sim = req.services.simulation(req.user_id)
    return Screen(T("menu.text", status=status), kb.main_menu(T, req.sign, demo_running=bool(sim and sim.is_running)))


async def help_screen(req: Request) -> Screen:
    T = req.T
    return Screen(T("menu.help") + "\n\n" + security_note(T.lang), kb.back_only(T, req.sign))


HANDLERS = {"m": main_menu, "h": help_screen}
