"""🤖 Ask Muse: preset questions answered from the caller's own positions (free-form questions stay on /ask)."""

from __future__ import annotations

from typing import Optional

from .. import keyboards as kb
from ..screen import Request, Screen

QUESTIONS = ("risk", "first", "pnl")


async def ask_menu(req: Request) -> Screen:
    T = req.T
    return Screen(T("ask.title") + "\n\n" + T("ask.text"), kb.ask_menu(T, req.sign, QUESTIONS))


async def answer(req: Request) -> Screen:
    s, T = req.services, req.T
    (key,) = req.args
    user = await s.user(req.user_id)
    snapshot = await s.snapshot(user)
    if not snapshot.positions:
        return Screen(T("ask.none"), kb.no_account(T, req.sign) if not snapshot.label else kb.ask_answer(T, req.sign))
    question = T(f"ask.q.{key}")
    instant = s.muse.answer_now(snapshot.positions, question, T.lang)

    def text(reply: str, live: bool) -> str:
        tag = T("alert.muse_mark" if live else "alert.muse_mark_mock")
        return f"{T('ask.answer', question=question)}\n\n{str(reply).strip()}\n\n{tag}"

    later = None
    if instant.pending is not None:
        pending = instant.pending

        async def later() -> Optional[str]:
            live = await pending
            return text(live, True) if live else None

    return Screen(text(instant.result, instant.live), kb.ask_answer(T, req.sign), later=later)


HANDLERS = {"qa": ask_menu, "qq": answer}
