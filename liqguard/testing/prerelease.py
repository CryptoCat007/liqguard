"""
Pre-release scenarios: the production aiogram Dispatcher on a fake Bot API, in English and 繁體中文.

Every scenario starts from a fresh database and drives real `Update`s through
`Dispatcher.feed_update`. A scenario fails on an unhandled update, a handler that does not
finish, a button that is never answered, an error toast, a body/button language mismatch, or
any demo message after the user pressed ✅ I'm awake / 😴 / ⏹ Stop demo / /stopdemo.

Run all of them with `python scripts/prerelease_check.py` (no Telegram network needed);
`tests/test_prerelease.py` runs the same scenarios under pytest.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from liqguard.ai import MetaLLMClient
from liqguard.config import Settings
from liqguard.core import AppContext
from liqguard.core.binding import BindingService
from liqguard.core.wicks import WickService
from liqguard.exchanges.base import KeyCheck
from liqguard.i18n import EN, ZH, Translator, catalog
from liqguard.models import ExchangeBinding
from liqguard.storage import EncryptedStorage
from liqguard.telegram.ui.codec import is_ui_payload
from liqguard.telegram.ui.router import ERROR, INVALID

from .telegram import FakeTelegram, Sent, UpdateFailed

USER = 617971023
TELEGRAM_CODE = {EN: "en", ZH: "zh-TW"}
API_KEY = "Kq7" + "x" * 50 + "AB12"
API_SECRET = "Sz9" + "y" * 50 + "CD34"
WALLET = "0x" + "ab" * 20
CJK = re.compile(r"[\u4e00-\u9fff]")
SYMBOL_LABEL = re.compile(r"^\W*[A-Z0-9]{2,12}(USDT)?\b")
NEUTRAL_LABELS = {v for k, v in catalog(EN).items() if k.startswith("kb.") and catalog(ZH).get(k) == v}
SETTLE = 0.4
QUIET_CHECKS = 4
SIM_OPTIONS = {"tick_interval": 0.03, "escalation_seconds_per_minute": 0.002}


class CheckFailed(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


class ReadOnlyKeyAdapter:
    """Accepts any well-formed key as read-only, without touching an exchange."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def check_key(self):
        return KeyCheck(True, "Read-only key accepted", ["READ"], True)


# --------------------------------------------------------------------------- session


class Session:
    """One fresh app (temporary database) and one Telegram user speaking `lang`."""

    def __init__(self, lang: str, tmp: Path):
        self.lang = lang
        self.T = Translator(lang)
        self.code = TELEGRAM_CODE[lang]
        self.tmp = tmp
        self.ctx: Optional[AppContext] = None
        self.tg: Optional[FakeTelegram] = None

    async def __aenter__(self) -> "Session":
        settings = Settings(telegram_token="123456:" + "A" * 35, db_path=str(self.tmp / "liqguard.db"),
                            key_file=self.tmp / "master.key", public_url="https://liqguard.test",
                            bot_username="liqguard_test_bot")
        storage = EncryptedStorage(db_path=settings.db_path, encryption_key=EncryptedStorage.generate_key())
        self.ctx = await AppContext.create(settings, storage=storage, llm=MetaLLMClient(use_mock=True),
                                           simulation_options=dict(SIM_OPTIONS), wicks=WickService(sources=[]))
        self.tg = FakeTelegram.create(self.ctx)
        self.tg.app.ui.services.binding = BindingService(
            self.ctx.storage, binance_factory=ReadOnlyKeyAdapter, hyperliquid_factory=ReadOnlyKeyAdapter,
            default_threshold=self.ctx.settings.default_threshold)
        await self.tg.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.tg.__aexit__(*exc)
        await self.ctx.close()

    # ---- actions

    async def command(self, text: str) -> Sent:
        mark = self.tg.mark()
        await self.tg.send_text(USER, text, self.code)
        sent = self.tg.to(USER, mark)
        check(bool(sent), f"{text}: no reply")
        return sent[-1]

    async def tap(self, message: Sent, data: str, what: str) -> Optional[Sent]:
        mark = self.tg.mark()
        await self.tg.tap(USER, message, data, self.code)
        toast = self.tg.answers()[-1].params.get("text") or ""
        check(toast not in (self.T(ERROR), self.T(INVALID)), f"{what}: error toast {toast!r}")
        sent = self.tg.to(USER, mark)
        return sent[-1] if sent else None

    async def tap_action(self, message: Sent, action: str, what: str) -> Optional[Sent]:
        data = self.button(message, action)
        check(data is not None, f"{what}: no {action!r} button on {[t for t, _ in message.buttons()]}")
        return await self.tap(message, data, what)

    def button(self, message: Sent, action: str) -> Optional[str]:
        codec = self.tg.app.ui.codec
        for _, data in message.buttons():
            if is_ui_payload(data) and codec.decode(USER, data).action == action:
                return data
        return None

    def actions(self, message: Sent) -> list[str]:
        codec = self.tg.app.ui.codec
        return [codec.decode(USER, d).action for _, d in message.buttons() if is_ui_payload(d)]

    def assert_language(self, message: Sent, what: str) -> None:
        """Body and buttons in the user's language (labels that read the same in both, like 🎬 Demo, pass)."""
        text = message.text.replace("語言", "").replace("繁體中文", "")
        labels = [t for t, _ in message.buttons() if t not in NEUTRAL_LABELS]
        if self.lang == EN:
            check(not CJK.search(text) and not any(CJK.search(x) for x in labels), f"{what}: Chinese in an English message")
        else:
            check(bool(CJK.search(text)), f"{what}: body not in 繁體中文")
            english = [x for x in labels if not CJK.search(x) and not SYMBOL_LABEL.match(x)]
            check(not english, f"{what}: English button on a 繁體中文 message: {english}")

    async def wait_for(self, predicate: Callable[[], object], what: str, timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            check(time.monotonic() < deadline, f"timed out waiting for {what}")
            await asyncio.sleep(0.01)

    def demo_messages(self, since: int = 0) -> list[Sent]:
        return self.tg.new_messages(USER, since)

    async def assert_quiet(self, since: int, what: str) -> None:
        """No new message to the user: the simulation has finished, the monitor polled again and every
        reminder that could be due (wall clock moved hours ahead, further on every check) was checked."""
        sim = self.ctx.simulations.get(USER)
        if sim is not None:
            await asyncio.wait_for(sim.wait(), 20)
        escalation = self.tg.app.escalation
        real_clock = escalation.clock
        try:
            for i in range(QUIET_CHECKS):
                escalation.clock = lambda hours=2 + 2 * i: real_clock() + hours * 3600
                await self.tg.app.monitor.check_all_users()
                await escalation.tick()
                await asyncio.sleep(SETTLE / QUIET_CHECKS)
        finally:
            escalation.clock = real_clock
        late = self.demo_messages(since)
        check(not late, f"{what}: {len(late)} demo message(s) after the user silenced the demo: "
                        f"{[m.text.splitlines()[0][:50] for m in late]}")

    async def main_menu(self) -> Sent:
        menu = await self.command("/start")
        check(len(self.actions(menu)) >= 8, f"/start: main menu has {len(menu.buttons())} buttons")
        self.assert_language(menu, "/start")
        return menu

    async def bind_wallet(self) -> None:
        await self.ctx.storage.save_binding(USER, ExchangeBinding(venue="hyperliquid", binding_type="wallet", wallet_address=WALLET))

    async def start_simulation(self) -> None:
        menu = await self.main_menu()
        demo = await self.tap_action(menu, "dm", "🎬 Demo")
        await self.tap(demo, self.button_with_arg(demo, "ds", "mixed"), "🎬 Liquidation sim")
        check(self.ctx.simulations.get(USER) is not None and self.ctx.simulations.get(USER).is_running,
              "🎬 Liquidation sim: simulation is not running")

    async def start_demo_book(self, scenario: str = "normal") -> Sent:
        """Bound user switches to a static demo book; returns the first monitor alert with ✅ I'm awake."""
        menu = await self.main_menu()
        await self.bind_wallet()
        demo = await self.tap_action(menu, "dm", "🎬 Demo")
        await self.tap(demo, self.button_with_arg(demo, "ds", scenario), f"demo scenario {scenario}")
        mark = self.tg.mark()
        for _ in range(10):
            await self.tg.app.monitor.check_all_users()
            alert = next((m for m in self.demo_messages(mark) if self.button(m, "aa")), None)
            if alert:
                return alert
        raise CheckFailed(f"demo book {scenario}: no alert with ✅ I'm awake after 10 monitor checks")

    def button_with_arg(self, message: Sent, action: str, arg: str) -> str:
        codec = self.tg.app.ui.codec
        for _, data in message.buttons():
            if is_ui_payload(data):
                d = codec.decode(USER, data)
                if d.action == action and d.args and d.args[0] == arg:
                    return data
        raise CheckFailed(f"no {action}({arg}) button on {[t for t, _ in message.buttons()]}")

    async def first_sim_alert(self) -> Sent:
        return await self.wait_for(lambda: next((m for m in self.demo_messages() if self.button(m, "aa")), None),
                                   "a simulation alert with ✅ I'm awake")


# --------------------------------------------------------------------------- scenarios


async def menu_buttons(s: Session) -> None:
    """/start, every main-menu button (and its screen's buttons, one level down), every command."""
    menu = await s.main_menu()
    for label, data in menu.buttons():
        screen = await s.tap(menu, data, f"main menu {label}")
        check(screen is not None, f"main menu {label}: nothing shown")
        if s.tg.app.ui.codec.decode(USER, data).action != "l":
            s.assert_language(screen, f"main menu {label}")
        for sub_label, sub in screen.buttons():
            if not is_ui_payload(sub):
                continue
            action = s.tg.app.ui.codec.decode(USER, sub).action
            if action in ("ls", "by", "do"):
                continue
            await s.tap(screen, sub, f"{label} → {sub_label}")
        await s.command("/start")
    for command in ("/menu", "/positions", "/risk", "/demo", "/stopdemo", "/settings", "/bind", "/dashboard", "/lang", "/help"):
        reply = await s.command(command)
        if command != "/lang":
            s.assert_language(reply, command)


async def language_switch(s: Session) -> None:
    """🌐 switches to the other language and every later screen follows it."""
    other = ZH if s.lang == EN else EN
    menu = await s.main_menu()
    picker = await s.tap_action(menu, "l", "🌐 Language")
    code = "zh" if other == ZH else "en"
    after = await s.tap(picker, s.button_with_arg(picker, "ls", code), "choose language")
    check(after is not None, "language: nothing shown after choosing")
    user = await s.ctx.storage.get_user(USER)
    check(user.language == other and user.language_explicit, f"language: saved {user.language}")
    s.lang, s.T = other, Translator(other)
    s.assert_language(after, "menu after switching language")


async def bind_flow(s: Session) -> None:
    """🔑 Bind account: Binance key + secret (both messages deleted), then a Hyperliquid wallet."""
    menu = await s.main_menu()
    bind = await s.tap_action(menu, "b", "🔑 Bind account")
    await s.tap_action(bind, "bb", "Bind Binance")
    await s.command(API_KEY)
    done = await s.command(API_SECRET)
    s.assert_language(done, "Binance bound")
    deleted = [c for c in s.tg.session.calls if c.method == "DeleteMessage"]
    check(len(deleted) >= 2, "bind: the key and secret messages were not deleted")
    check(not any(API_SECRET in m.text or API_KEY in m.text for m in s.tg.session.sent), "bind: the key was echoed back")
    bind = await s.tap_action(await s.main_menu(), "b", "🔑 Bind account")
    await s.tap_action(bind, "bh", "Bind Hyperliquid")
    await s.command(WALLET)
    venues = {b["venue"] for b in await s.tg.app.ui.services.binding.list(USER)}
    check(venues == {"binance", "hyperliquid"}, f"bind: bound venues {venues}")


async def full_demo_run(s: Session) -> None:
    """An untouched simulation ends on its own; every alert and reminder carried ⏹ Stop demo; then silence."""
    await s.start_simulation()
    sim = s.ctx.simulations.get(USER)
    await asyncio.wait_for(sim.wait(), 20)
    check(sim.state == "finished", f"demo run: state {sim.state}")
    with_buttons = [m for m in s.demo_messages() if s.button(m, "aa")]
    check(bool(with_buttons), "demo run: no alert reached the user")
    for m in with_buttons:
        check(s.button(m, "dx") is not None, f"demo alert without ⏹ Stop demo: {m.text.splitlines()[0][:50]}")
        s.assert_language(m, "demo alert")
    await s.assert_quiet(s.tg.mark(), "after the demo finished")


def silence_the_simulation(action: str) -> Callable[[Session], Awaitable[None]]:
    async def scenario(s: Session) -> None:
        await s.start_simulation()
        alert = await s.first_sim_alert()
        check(s.button(alert, "dx") is not None, "simulation alert without ⏹ Stop demo")
        s.assert_language(alert, "simulation alert")
        if action == "/stopdemo":
            await s.command("/stopdemo")
        else:
            await s.tap_action(alert, action, f"simulation alert {action}")
        await s.assert_quiet(s.tg.mark(), f"simulation after {action}")
    scenario.__doc__ = f"Start the simulation, then {action} on its first alert: nothing more arrives."
    return scenario


def silence_the_demo_book(action: str) -> Callable[[Session], Awaitable[None]]:
    async def scenario(s: Session) -> None:
        alert = await s.start_demo_book()
        check(s.button(alert, "dx") is not None, f"demo-book alert without ⏹ Stop demo: {s.actions(alert)}")
        s.assert_language(alert, "demo-book alert")
        if action == "/stopdemo":
            await s.command("/stopdemo")
        else:
            await s.tap_action(alert, action, f"demo-book alert {action}")
        await s.assert_quiet(s.tg.mark(), f"demo book after {action}")
    scenario.__doc__ = f"Bound user on a static demo book, then {action} on the first monitor alert: nothing more arrives."
    return scenario


async def reminder_has_stop_button(s: Session) -> None:
    """A demo wake-up reminder carries ⏹ Stop demo, and pressing it ends everything."""
    await s.start_demo_book()
    escalation = s.tg.app.escalation
    real_clock = escalation.clock
    mark = s.tg.mark()
    escalation.clock = lambda: real_clock() + 3600
    try:
        await escalation.tick()
    finally:
        escalation.clock = real_clock
    reminders = s.demo_messages(mark)
    check(bool(reminders), "no demo reminder was sent")
    reminder = reminders[-1]
    check(s.button(reminder, "dx") is not None, f"demo reminder without ⏹ Stop demo: {s.actions(reminder)}")
    s.assert_language(reminder, "demo reminder")
    await s.tap_action(reminder, "dx", "reminder ⏹ Stop demo")
    await s.assert_quiet(s.tg.mark(), "after ⏹ on a reminder")


async def restart_is_quiet(s: Session) -> None:
    """A user left in demo mode gets nothing from the demo book after a restart until they pick a scenario."""
    await s.main_menu()
    await s.bind_wallet()
    await s.ctx.storage.set_demo_mode(USER, True)
    await s.assert_quiet(s.tg.mark(), "after a restart in demo mode")


SCENARIOS: dict[str, Callable[[Session], Awaitable[None]]] = {
    "menu-buttons": menu_buttons,
    "language-switch": language_switch,
    "bind-flow": bind_flow,
    "full-demo-run": full_demo_run,
    "sim-awake": silence_the_simulation("aa"),
    "sim-snooze": silence_the_simulation("az"),
    "sim-stop-button": silence_the_simulation("dx"),
    "sim-stopdemo-command": silence_the_simulation("/stopdemo"),
    "book-awake": silence_the_demo_book("aa"),
    "book-snooze": silence_the_demo_book("az"),
    "book-stop-button": silence_the_demo_book("dx"),
    "book-stopdemo-command": silence_the_demo_book("/stopdemo"),
    "reminder-stop-button": reminder_has_stop_button,
    "restart-is-quiet": restart_is_quiet,
}


@dataclass
class Result:
    scenario: str
    lang: str
    ok: bool
    detail: str
    seconds: float


async def run_one(name: str, lang: str) -> Result:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="liqguard-prerelease-") as tmp:
        try:
            async with Session(lang, Path(tmp)) as s:
                await SCENARIOS[name](s)
                unhandled = s.tg.unhandled
            ok, detail = not unhandled, f"unhandled updates: {unhandled}" if unhandled else ""
        except (CheckFailed, UpdateFailed) as e:
            ok, detail = False, str(e)
        except Exception as e:  # noqa: BLE001 - a crash is a failed check, reported by type
            ok, detail = False, f"crashed: {type(e).__name__}: {e}"
    return Result(name, lang, ok, detail, time.monotonic() - started)


async def run_all(names: Optional[list[str]] = None, langs: tuple[str, ...] = (EN, ZH),
                  report: Optional[Callable[[Result], None]] = None) -> list[Result]:
    results = []
    for name in names or list(SCENARIOS):
        for lang in langs:
            result = await run_one(name, lang)
            results.append(result)
            if report:
                report(result)
    return results


__all__ = ["SCENARIOS", "Result", "run_all", "run_one"]
