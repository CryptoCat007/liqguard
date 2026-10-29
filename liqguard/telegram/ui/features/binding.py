"""
🔑 綁定帳號: guided self-serve binding.

Credentials arrive as chat messages while the user is in a binding state. The
message is deleted before anything else happens, the key is checked against
the exchange (keys with trade/withdraw/transfer permission are rejected) and
saved encrypted with the user's own data key. Replies only ever show masked
hints; nothing typed by the user is echoed back.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup

from liqguard.core.binding import SECURITY_NOTE, looks_like_secret_material, split_key_secret

from .. import keyboards as kb
from ..screen import Request, Screen

CANCEL_WORDS = {"取消", "cancel", "/cancel", "/menu", "/start"}
VENUE_LABEL = {"binance": "🟡 Binance", "hyperliquid": "🟢 Hyperliquid"}
PRIVATE_ONLY = "🔒 為了安全，請在與 LiqGuard 的私訊中綁定帳號（群組裡的訊息別人看得到）。"

BINANCE_GUIDE = (
    "🟡 綁定 Binance（唯讀 API 金鑰）\n\n"
    "1️⃣ 登入 Binance → 右上角頭像 →「API 管理」\n"
    "2️⃣ 建立 API →「系統生成」，取個名字（例如 LiqGuard）\n"
    "3️⃣ 權限只勾 ✅「允許讀取 Enable Reading」\n"
    "    ❌ 不要勾：合約交易、現貨與槓桿交易、允許提現、萬向劃轉\n"
    "4️⃣ （建議）設定 IP 存取限制\n"
    "5️⃣ 把 API Key 和 Secret Key 用空白或換行隔開，一次傳給我\n"
    "    （也可以先傳 Key，再傳 Secret）\n\n"
    "🧹 我讀取後會立刻刪除你的訊息；有交易或提現權限的金鑰會被拒絕。\n"
    "⛔ 絕對不要傳私鑰、助記詞或登入密碼。"
)
HYPERLIQUID_GUIDE = (
    "🟢 綁定 Hyperliquid（只需要錢包地址）\n\n"
    "請傳送你的錢包地址：0x 開頭、共 42 個字元。\n"
    "LiqGuard 只讀取公開的倉位資料，不需要也不接受任何金鑰。\n\n"
    "⛔ 絕對不要傳私鑰或助記詞（12/24 個英文單字）。"
)


class BindStates(StatesGroup):
    binance_key = State()
    binance_secret = State()
    hyperliquid_address = State()


def _label(b: dict) -> str:
    venue = VENUE_LABEL.get(b["venue"], b["venue"])
    net = " 測試網" if b.get("network") == "testnet" else ""
    hint = b.get("api_key") or b.get("wallet_address") or ""
    return f"{venue}{net} {hint}".strip()


async def menu(req: Request) -> Screen:
    if req.state is not None:
        await req.state.clear()
    bindings = await req.services.binding.list(req.user_id)
    bound = "\n".join(f"• {_label(b)}" for b in bindings) or "• 尚未綁定"
    return Screen(f"🔑 綁定帳號\n\n目前綁定：\n{bound}\n\n{SECURITY_NOTE}", kb.binding_menu(req.sign))


async def _start(req: Request, state: State, guide: str) -> Screen:
    if req.chat_type != "private":
        return Screen(PRIVATE_ONLY, kb.binding_back(req.sign), toast="請改用私訊", alert=True)
    if req.state is None:
        return Screen("⚠️ 目前無法開始綁定，請輸入 /menu 後再試。", kb.binding_back(req.sign))
    await req.state.clear()
    await req.state.set_state(state)
    return Screen(guide, kb.binding_waiting(req.sign))


async def start_binance(req: Request) -> Screen:
    return await _start(req, BindStates.binance_key, BINANCE_GUIDE)


async def start_hyperliquid(req: Request) -> Screen:
    return await _start(req, BindStates.hyperliquid_address, HYPERLIQUID_GUIDE)


async def show_bound(req: Request) -> Screen:
    bindings = await req.services.binding.list(req.user_id)
    if not bindings:
        return Screen("📋 查看已綁定\n\n尚未綁定任何帳號。", kb.binding_back(req.sign))
    lines = []
    for b in bindings:
        perms = "、".join(b.get("permissions") or []) or "唯讀"
        lines.append(f"• {_label(b)}\n  權限：{perms}")
    return Screen("📋 查看已綁定（金鑰只顯示末四碼）\n\n" + "\n".join(lines), kb.binding_back(req.sign))


async def unbind_list(req: Request) -> Screen:
    bindings = await req.services.binding.list(req.user_id)
    if not bindings:
        return Screen("🗑 解除綁定\n\n目前沒有可解除的綁定。", kb.binding_back(req.sign))
    items = [(b["venue"], b.get("network") or "mainnet", _label(b)) for b in bindings]
    return Screen("🗑 解除綁定\n\n請選擇要解除的帳號：", kb.unbind_list(req.sign, items))


async def unbind_confirm(req: Request) -> Screen:
    venue, network = req.args
    bindings = await req.services.binding.list(req.user_id)
    match = next((b for b in bindings if b["venue"] == venue and (b.get("network") or "mainnet") == network), None)
    if match is None:
        return Screen("這個綁定已不存在。", kb.binding_back(req.sign), toast="綁定已不存在")
    return Screen(
        f"⚠️ 確定要解除 {_label(match)}？\n\n解除後會刪除加密保存的金鑰／地址，並停止監控這個帳號。\n（確認按鈕 2 分鐘內有效）",
        kb.unbind_confirm(req.sign, venue, network),
    )


async def unbind_do(req: Request) -> Screen:
    venue, network = req.args
    ok = await req.services.binding.unbind(req.user_id, venue, network)
    text = f"✅ 已解除 {VENUE_LABEL[venue]}{' 測試網' if network == 'testnet' else ''} 的綁定，金鑰已刪除。" if ok else "這個綁定已不存在。"
    return Screen(text, kb.binding_back(req.sign), toast="已解除綁定" if ok else "綁定已不存在")


async def cancel(req: Request) -> Screen:
    if req.state is not None:
        await req.state.clear()
    screen = await menu(req)
    screen.toast = "已取消綁定"
    return screen


async def _delete(message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def on_message(services, message, state, sign) -> Screen:
    """Text sent while in a binding state. Deleted first; only masked results are ever shown."""
    text = (message.text or "").strip()
    await _delete(message)
    current = await state.get_state()
    if text.lower() in CANCEL_WORDS:
        await state.clear()
        return Screen("已取消綁定。", kb.binding_back(sign), new_message=True)
    if getattr(getattr(message, "chat", None), "type", "private") != "private":
        await state.clear()
        return Screen(PRIVATE_ONLY, kb.binding_back(sign), new_message=True)
    uid = message.from_user.id
    deleted = "🧹 你的訊息已刪除。\n"

    if current == BindStates.hyperliquid_address.state:
        result = await services.binding.bind_hyperliquid(uid, text)
        if result.ok:
            await state.clear()
            return Screen(f"{deleted}{result.message}", kb.binding_back(sign), new_message=True)
        return Screen(f"{deleted}{result.message}\n\n請重新傳送錢包地址，或按「取消綁定」。", kb.binding_waiting(sign), new_message=True)

    if current == BindStates.binance_key.state:
        if looks_like_secret_material(text, bare_hex=False):
            return Screen(f"{deleted}⛔ 這看起來像私鑰或助記詞！請勿外流。LiqGuard 只需要 Binance 唯讀 API 金鑰。",
                          kb.binding_waiting(sign), new_message=True)
        key, secret = split_key_secret(text)
        if key is None:
            return Screen(f"{deleted}❌ 請只傳 API Key 和 Secret（用空白或換行隔開）。", kb.binding_waiting(sign), new_message=True)
        if secret is None:
            await state.update_data(api_key=key)
            await state.set_state(BindStates.binance_secret)
            return Screen(f"{deleted}✅ 已收到 API Key。\n\n現在請傳送 Secret Key：", kb.binding_waiting(sign), new_message=True)
    elif current == BindStates.binance_secret.state:
        key, secret = (await state.get_data()).get("api_key"), text
        if not key:
            await state.set_state(BindStates.binance_key)
            return Screen(f"{deleted}⌛ 流程已逾時，請重新傳送 API Key 和 Secret。", kb.binding_waiting(sign), new_message=True)
    else:
        await state.clear()
        return Screen("已結束綁定流程。", kb.binding_back(sign), new_message=True)

    await state.update_data(api_key=None)
    result = await services.binding.bind_binance(uid, key, secret)
    if result.ok:
        await state.clear()
        return Screen(f"{deleted}{result.message}\n\n可以到 📊 我的倉位 查看了。", kb.binding_back(sign), new_message=True)
    await state.set_state(BindStates.binance_key)
    return Screen(f"{deleted}{result.message}\n\n請重新傳送 API Key 和 Secret，或按「取消綁定」。", kb.binding_waiting(sign), new_message=True)


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
