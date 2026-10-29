"""Alert buttons: ✅ 我醒了 and 😴 暫停提醒 30 分鐘 (🚑 先處理這個 is the positions feature's 救倉試算)."""

from __future__ import annotations

from .. import keyboards as kb
from ..screen import Request, Screen

SNOOZE_MINUTES = 30
GONE = "這則警報已確認或已失效。"


async def acknowledge(req: Request) -> Screen:
    symbol = await req.services.acknowledge(req.user_id, req.args[0])
    if symbol is None:
        return Screen(GONE, toast=GONE, edit=False)
    return Screen(f"✅ 已確認，{symbol} 的喚醒提醒已停止。注意安全！", kb.back_only(req.sign),
                  toast="已停止提醒", new_message=True, strip_keyboard=True)


async def snooze(req: Request) -> Screen:
    symbol = await req.services.snooze(req.user_id, req.args[0], SNOOZE_MINUTES)
    if symbol is None:
        return Screen(GONE, toast=GONE, edit=False)
    return Screen(f"😴 {symbol} 的提醒暫停 {SNOOZE_MINUTES} 分鐘；之後若仍危險會再叫醒你。", kb.back_only(req.sign),
                  toast=f"已暫停 {SNOOZE_MINUTES} 分鐘", new_message=True)


HANDLERS = {"aa": acknowledge, "az": snooze}
