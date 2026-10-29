"""🎬 Demo: static demo books and the private, finite market simulation, all driven from buttons."""

from __future__ import annotations

from .. import keyboards as kb
from ..screen import Request, Screen

STATIC = ("normal", "volatile", "critical", "safe")


async def menu(req: Request) -> Screen:
    T = req.T
    user = await req.services.user(req.user_id)
    sim = req.services.simulation(req.user_id)
    running = sim is not None and sim.is_running
    text = T("demo.intro") + "\n\n" + (T("demo.running") if running else T("demo.choose"))
    return Screen(text, kb.demo_menu(T, req.sign, running, user.is_demo_mode))


async def scenario(req: Request) -> Screen:
    key, s, T = req.args[0], req.services, req.T
    if key in STATIC:
        await s.stop_demo(req.user_id)
        await s.set_demo_mode(req.user_id, True, key)
        return Screen(T("demo.static_on", name=T(f"demo.scenario.{key}")), kb.demo_started(T, req.sign),
                      toast=T("demo.static_on_toast"))
    await s.set_demo_mode(req.user_id, True)
    user = await s.user(req.user_id)
    sim = s.simulation_for(user)
    sim.set_language(T.lang)
    sim.set_scenario(key)
    started = await sim.start(source="telegram")
    text = (T("demo.sim_title", name=T(f"demo.scenario.{key}")) + "\n\n"
            + (T("demo.sim_started") if started else T("demo.sim_already")) + "\n" + T("demo.sim_body"))
    return Screen(text, kb.demo_started(T, req.sign), toast=T("demo.sim_started_toast") if started else T("demo.sim_already_toast"))


async def stop(req: Request) -> Screen:
    """⏹ Stop demo (menu, every demo message, /stopdemo): the run and all its reminders end now."""
    T = req.T
    was_running = await req.services.stop_demo(req.user_id)
    screen = await menu(req)
    screen.text = T("demo.stopped" if was_running else "demo.not_running") + "\n\n" + screen.text
    screen.toast = T("demo.stopped_toast")
    screen.new_message, screen.strip_keyboard = True, True
    return screen


async def disable(req: Request) -> Screen:
    T = req.T
    await req.services.stop_demo(req.user_id)
    await req.services.set_demo_mode(req.user_id, False)
    return Screen(T("demo.disabled"), kb.main_menu(T, req.sign), toast=T("demo.disabled_toast"))


HANDLERS = {"dm": menu, "ds": scenario, "dx": stop, "do": disable}
