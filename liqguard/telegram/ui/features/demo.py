"""🎬 Demo: static demo books and the private market simulation, all driven from buttons."""

from __future__ import annotations

from liqguard.simulation import SCENARIOS as SIM_SCENARIOS

from .. import keyboards as kb
from ..screen import Request, Screen

STATIC = {"normal": "😊 一般行情", "volatile": "📈 劇烈波動", "critical": "🚨 瀕臨強平", "safe": "✅ 安全組合"}
SIMULATED = {"mixed": "強平模擬", "cross_longs": "5 個多單同錢包（全倉）"}

INTRO = (
    "🎬 Demo 模式\n\n"
    "用模擬倉位體驗 LiqGuard，不需要綁定帳號：\n"
    "• 一般行情／劇烈波動／瀕臨強平／安全組合：固定的示範倉位\n"
    "• 🎬 強平模擬：價格一步步走向強平，警報、喚醒提醒都會發到這裡\n"
    "• 🎬 5 個多單同錢包：五個相關的全倉多單一起下跌，看哪個該先救、帳戶在跌多少時爆倉\n\n"
    "模擬只屬於你，不會影響任何真實帳戶。"
)


async def menu(req: Request) -> Screen:
    user = await req.services.user(req.user_id)
    sim = req.services.simulation(req.user_id)
    running = sim is not None and sim.is_running
    text = INTRO + ("\n\n▶️ 模擬進行中，可以隨時查看倉位或停止。" if running else "\n\n請選擇情境：")
    return Screen(text, kb.demo_menu(req.sign, running, user.is_demo_mode))


async def scenario(req: Request) -> Screen:
    key = req.args[0]
    s = req.services
    if key in STATIC:
        sim = s.simulation(req.user_id)
        if sim is not None and sim.is_running:
            await sim.stop(source="telegram")
        await s.set_demo_mode(req.user_id, True, key)
        return Screen(f"🎮 已啟用 Demo：{STATIC[key]}\n\n下方按鈕都會使用這組模擬倉位。", kb.demo_started(req.sign), toast="Demo 已啟用")
    await s.set_demo_mode(req.user_id, True)
    user = await s.user(req.user_id)
    sim = s.simulation_for(user)
    sim.set_scenario(key)
    started = await sim.start(source="telegram")
    title = SIM_SCENARIOS[sim.scenario].title if sim.scenario in SIM_SCENARIOS else SIMULATED[key]
    text = (
        f"🎬 市場模擬 · {SIMULATED.get(key, title)}\n\n"
        + ("已開始。" if started else "模擬已在進行中。")
        + "價格會逐步走向強平，警報會傳到這裡（也會出現在你的網頁看板）。\n"
        "收到警報時可以點 🚑 先處理這個、✅ 我醒了 或 😴 暫停提醒 30 分鐘。"
    )
    return Screen(text, kb.demo_started(req.sign), toast="模擬開始" if started else "模擬進行中")


async def stop(req: Request) -> Screen:
    sim = req.services.simulation(req.user_id)
    if sim is not None:
        await sim.stop(source="telegram")
    screen = await menu(req)
    screen.text = "⏹ 模擬已停止。\n\n" + screen.text
    screen.toast = "已停止"
    return screen


async def disable(req: Request) -> Screen:
    sim = req.services.simulation(req.user_id)
    if sim is not None:
        await sim.stop(source="telegram")
    await req.services.set_demo_mode(req.user_id, False)
    return Screen("📊 已關閉 Demo 模式，之後會顯示你綁定帳號的真實倉位。", kb.main_menu(req.sign), toast="Demo 已關閉")


HANDLERS = {"dm": menu, "ds": scenario, "dx": stop, "do": disable}
