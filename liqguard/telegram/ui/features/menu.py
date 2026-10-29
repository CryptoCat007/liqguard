"""🏠 主選單 and ❓ 幫助."""

from __future__ import annotations

from liqguard.core.binding import SECURITY_NOTE

from .. import keyboards as kb
from ..screen import Request, Screen

HELP = (
    "❓ 幫助\n\n"
    "LiqGuard 盯住你的永續合約倉位，在被強平之前提醒你，並算出最便宜的救倉方式。\n\n"
    "📊 我的倉位：每個倉位下方有 救倉試算 / 插針壓測 / 止損檢查 / 資金費影響\n"
    "🚨 先處理哪個：多個倉位同時告急時，依緊急程度排序並說明原因\n"
    "🧪 壓力測試：同一錢包所有倉位一起下跌時，帳戶在跌多少 % 時被強平\n"
    "💸 資金費率/手續費：下一次結算要付多少、平倉手續費\n"
    "🔔 通知設定：警示門檻、喚醒提醒間隔、勿擾時段\n"
    "🔑 綁定帳號：Binance 唯讀 API 金鑰或 Hyperliquid 錢包地址\n"
    "🖥️ 網頁看板：一次性登入連結\n"
    "🎬 Demo：用模擬倉位體驗所有功能\n\n"
    "收到警報時：🚑 先處理這個 直接開啟救倉試算，✅ 我醒了 停止喚醒提醒，😴 暫停提醒 30 分鐘。\n\n"
    "指令：/menu 主選單 · /positions 倉位 · /risk 風險總覽 · /demo · /settings · /dashboard\n\n"
    + SECURITY_NOTE
)


async def main_menu(req: Request) -> Screen:
    user = await req.services.user(req.user_id)
    active = [b for b in user.bindings if b.is_active]
    if user.is_demo_mode:
        status = "🎮 目前為 Demo 模式（模擬資料）"
    elif active:
        names = "、".join(sorted({b.venue.title() + (" 測試網" if b.is_testnet else "") for b in active}))
        status = f"🔗 已綁定：{names}"
    else:
        status = "🔌 尚未綁定帳號，可先試試 🎬 Demo"
    text = (
        "🏠 主選單\n\n"
        "LiqGuard 幫你盯住永續合約的強平風險：先告訴你該處理哪一個，再算出最便宜的救法。\n\n"
        f"{status}\n\n請選擇功能："
    )
    return Screen(text, kb.main_menu(req.sign))


async def help_screen(req: Request) -> Screen:
    return Screen(HELP, kb.back_only(req.sign))


HANDLERS = {"m": main_menu, "h": help_screen}
