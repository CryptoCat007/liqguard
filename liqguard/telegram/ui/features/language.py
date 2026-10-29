"""🌐 Language / 語言: pick English or 繁體中文; the choice is saved per user and wins over the Telegram app language."""

from __future__ import annotations

from liqguard.i18n import LANGUAGE_NAMES, Translator

from .. import keyboards as kb
from ..screen import Request, Screen

CODES = {"en": "en", "zh": "zh-Hant"}


async def picker(req: Request) -> Screen:
    T = req.T
    user = await req.services.user(req.user_id)
    text = T("lang.picker_title") + "\n\n" + T("lang.current", name=LANGUAGE_NAMES[T.lang])
    if not user.language_explicit:
        text += "\n" + T("lang.auto_note")
    return Screen(text, kb.language_menu(T, req.sign))


async def choose(req: Request) -> Screen:
    lang = await req.services.set_language(req.user_id, CODES[req.args[0]])
    T = Translator(lang)
    user = await req.services.user(req.user_id)
    status = T("menu.status_demo") if user.is_demo_mode else T("menu.status_bound", names=T.join(
        sorted({b.venue.title() for b in user.bindings if b.is_active}))) if user.has_any_binding() else T("menu.status_unbound")
    return Screen(T("lang.set") + "\n\n" + T("menu.text", status=status), kb.main_menu(T, req.sign), toast=T("lang.set_toast"))


HANDLERS = {"l": picker, "ls": choose}
