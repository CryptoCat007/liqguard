"""🖥️ 網頁看板: one-time login link, private chats only."""

from __future__ import annotations

from .. import keyboards as kb
from ..screen import Request, Screen


async def dashboard(req: Request) -> Screen:
    if req.chat_type != "private":
        return Screen("🔒 看板連結是個人專屬的，請在與 LiqGuard 的私訊中點「🖥️ 網頁看板」。", kb.back_only(req.sign),
                      toast="請改用私訊", alert=True)
    link = await req.services.dashboard_link(req.user_id)
    if link is None:
        return Screen("🖥️ 這個 LiqGuard 沒有啟用網頁看板，所有功能都可以在這裡使用。", kb.back_only(req.sign))
    url, minutes = link
    text = (
        "🖥️ 你的專屬 LiqGuard 看板\n\n"
        f"{url}\n\n"
        f"• 只能使用一次，{minutes} 分鐘內有效\n"
        "• 只會顯示你自己的倉位、警報與紀錄\n"
        "• 請勿分享：連結被使用前，任何人都能用它登入成你\n"
        "• 輸入 /dashboard revoke 可登出所有裝置"
    )
    return Screen(text, kb.dashboard(req.sign, url), new_message=True)


HANDLERS = {"db": dashboard}
