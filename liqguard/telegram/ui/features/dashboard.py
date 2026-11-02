"""🖥️ Dashboard: one-time login link, private chats only."""

from __future__ import annotations

from .. import keyboards as kb
from ..screen import Request, Screen


async def dashboard(req: Request) -> Screen:
    T = req.T
    if req.chat_type != "private":
        return Screen(T("dashboard.private_only"), kb.back_only(T, req.sign), toast=T("common.use_private"), alert=True)
    link = await req.services.dashboard_link(req.user_id, T.lang)
    if link is None:
        return Screen(T("dashboard.disabled"), kb.back_only(T, req.sign))
    url, minutes = link
    return Screen(T("dashboard.link", url=url, minutes=minutes), kb.dashboard(T, req.sign, url), new_message=True)


HANDLERS = {"db": dashboard}
