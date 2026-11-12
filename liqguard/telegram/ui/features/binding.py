"""
🔑 Bind account: guided self-serve binding.

Credentials arrive as chat messages while the user is in a binding state. The
message is deleted before anything else happens, the key is checked against
the exchange (keys with trade/withdraw/transfer permission are rejected) and
saved encrypted with the user's own data key. Replies only ever show masked
hints; nothing typed by the user is echoed back.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup

from liqguard.core.binding import looks_like_secret_material, security_note, split_key_secret
from liqguard.i18n import Translator

from .. import keyboards as kb
from ..screen import Request, Screen

CANCEL_WORDS = {"取消", "cancel", "/cancel", "/menu", "/start"}
VENUE_ICON = {"binance": "🟡", "hyperliquid": "🟢"}


class BindStates(StatesGroup):
    binance_key = State()
    binance_secret = State()
    hyperliquid_address = State()


def _venue(T: Translator, venue: str, network: str = "mainnet") -> str:
    return f"{VENUE_ICON.get(venue, '')} {T(f'common.venue.{venue}')}{T('common.testnet') if network == 'testnet' else ''}".strip()


def _label(T: Translator, b: dict) -> str:
    hint = b.get("api_key") or b.get("wallet_address") or ""
    return f"{_venue(T, b['venue'], b.get('network') or 'mainnet')} {hint}".strip()


async def menu(req: Request) -> Screen:
    T = req.T
    if req.state is not None:
        await req.state.clear()
    bindings = await req.services.binding.list(req.user_id)
    bound = "\n".join(f"• {_label(T, b)}" for b in bindings) or T("binding.none_bullet")
    return Screen(T("binding.menu", bound=bound) + "\n\n" + security_note(T.lang), kb.binding_menu(T, req.sign))


async def _start(req: Request, state: State, guide: str) -> Screen:
    T = req.T
    if req.chat_type != "private":
        return Screen(T("binding.private_only"), kb.binding_back(T, req.sign), toast=T("common.use_private"), alert=True)
    if req.state is None:
        return Screen(T("binding.cannot_start"), kb.binding_back(T, req.sign))
    await req.state.clear()
    await req.state.set_state(state)
    return Screen(T(guide), kb.binding_waiting(T, req.sign))


async def start_binance(req: Request) -> Screen:
    blocked = await req.services.account_limit_screen(req, "binance")
    if blocked is not None:
        return blocked
    return await _start(req, BindStates.binance_key, "binding.guide_binance")


async def start_hyperliquid(req: Request) -> Screen:
    blocked = await req.services.account_limit_screen(req, "hyperliquid")
    if blocked is not None:
        return blocked
    return await _start(req, BindStates.hyperliquid_address, "binding.guide_hyperliquid")


async def show_bound(req: Request) -> Screen:
    T = req.T
    bindings = await req.services.binding.list(req.user_id)
    if not bindings:
        return Screen(T("binding.bound_empty"), kb.binding_back(T, req.sign))
    lines = [T("binding.bound_item", label=_label(T, b), permissions=T.join(b.get("permissions") or []) or T("binding.read_only"))
             for b in bindings]
    return Screen(T("binding.bound_title") + "\n\n" + "\n".join(lines), kb.binding_back(T, req.sign))


async def unbind_list(req: Request) -> Screen:
    T = req.T
    bindings = await req.services.binding.list(req.user_id)
    if not bindings:
        return Screen(T("binding.unbind_empty"), kb.binding_back(T, req.sign))
    items = [(b["venue"], b.get("network") or "mainnet", _label(T, b)) for b in bindings]
    return Screen(T("binding.unbind_pick"), kb.unbind_list(T, req.sign, items))


async def unbind_confirm(req: Request) -> Screen:
    (venue, network), T = req.args, req.T
    bindings = await req.services.binding.list(req.user_id)
    match = next((b for b in bindings if b["venue"] == venue and (b.get("network") or "mainnet") == network), None)
    if match is None:
        return Screen(T("binding.gone"), kb.binding_back(T, req.sign), toast=T("binding.gone_toast"))
    return Screen(T("binding.unbind_confirm", label=_label(T, match)), kb.unbind_confirm(T, req.sign, venue, network))


async def unbind_do(req: Request) -> Screen:
    (venue, network), T = req.args, req.T
    ok = await req.services.binding.unbind(req.user_id, venue, network)
    text = T("binding.unbound", venue=_venue(T, venue, network)) if ok else T("binding.gone")
    return Screen(text, kb.binding_back(T, req.sign), toast=T("binding.unbound_toast") if ok else T("binding.gone_toast"))


async def cancel(req: Request) -> Screen:
    if req.state is not None:
        await req.state.clear()
    screen = await menu(req)
    screen.toast = req.T("binding.cancelled_toast")
    return screen


async def _delete(message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def on_message(services, message, state, sign, T: Translator) -> Screen:
    """Text sent while in a binding state. Deleted first; only masked results are ever shown."""
    text = (message.text or "").strip()
    await _delete(message)
    current = await state.get_state()
    if text.lower() in CANCEL_WORDS:
        await state.clear()
        return Screen(T("binding.cancelled"), kb.binding_back(T, sign), new_message=True)
    if getattr(getattr(message, "chat", None), "type", "private") != "private":
        await state.clear()
        return Screen(T("binding.private_only"), kb.binding_back(T, sign), new_message=True)
    uid = message.from_user.id
    deleted = T("binding.deleted") + "\n"

    if current == BindStates.hyperliquid_address.state:
        result = await services.binding.bind_hyperliquid(uid, text, lang=T.lang)
        if result.ok:
            await state.clear()
            return Screen(f"{deleted}{result.message}", kb.binding_back(T, sign), new_message=True)
        if result.code == "account_limit":
            await state.clear()
            return Screen(f"{deleted}{result.message}", kb.upsell(T, sign), new_message=True)
        return Screen(f"{deleted}{result.message}\n\n{T('binding.retry_wallet')}", kb.binding_waiting(T, sign), new_message=True)

    if current == BindStates.binance_key.state:
        if looks_like_secret_material(text, bare_hex=False):
            return Screen(f"{deleted}{T('binding.secret_material')}", kb.binding_waiting(T, sign), new_message=True)
        key, secret = split_key_secret(text)
        if key is None:
            return Screen(f"{deleted}{T('binding.key_secret_only')}", kb.binding_waiting(T, sign), new_message=True)
        if secret is None:
            await state.update_data(api_key=key)
            await state.set_state(BindStates.binance_secret)
            return Screen(f"{deleted}{T('binding.key_received')}", kb.binding_waiting(T, sign), new_message=True)
    elif current == BindStates.binance_secret.state:
        key, secret = (await state.get_data()).get("api_key"), text
        if not key:
            await state.set_state(BindStates.binance_key)
            return Screen(f"{deleted}{T('binding.expired')}", kb.binding_waiting(T, sign), new_message=True)
    else:
        await state.clear()
        return Screen(T("binding.ended"), kb.binding_back(T, sign), new_message=True)

    await state.update_data(api_key=None)
    result = await services.binding.bind_binance(uid, key, secret, lang=T.lang)
    if result.ok:
        await state.clear()
        return Screen(f"{deleted}{result.message}\n\n{T('binding.see_positions')}", kb.binding_back(T, sign), new_message=True)
    if result.code == "account_limit":
        await state.clear()
        return Screen(f"{deleted}{result.message}", kb.upsell(T, sign), new_message=True)
    await state.set_state(BindStates.binance_key)
    return Screen(f"{deleted}{result.message}\n\n{T('binding.retry_binance')}", kb.binding_waiting(T, sign), new_message=True)


HANDLERS = {
    "b": menu,
    "bb": start_binance,
    "bh": start_hyperliquid,
    "bl": show_bound,
    "bu": unbind_list,
    "bx": unbind_confirm,
    "by": unbind_do,
    "bc": cancel,
}
