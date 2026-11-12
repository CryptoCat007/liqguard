"""
No command is reachable only by typing: every slash command the production
dispatcher registers has an inline-keyboard route (`MENU_ROUTES`), and that
route is a button a user reaches by tapping from 🏠 Main menu, in English and
繁體中文. Also pins the demo path to 🛑 Stop-loss check and the new screens.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters import Command

from liqguard.i18n import EN, LANGUAGES, ZH, Translator
from liqguard.models import User
from liqguard.telegram.app import wire_bot
from liqguard.telegram.ui import BOT_COMMANDS, MENU_ROUTES
from liqguard.telegram.ui import keyboards as kb
from liqguard.telegram.ui.actions import ACTIONS

from .conftest import ALICE, fake_message
from .test_telegram_ui import enable_demo, has_chinese
from .zh_scan import english_words

SIDE_EFFECTS = frozenset({"ls", "ss", "bb", "bh", "bx", "by", "bc", "ds", "dx", "do", "aa", "az", "ak", "ay", "an", "db",
                          "pu", "pm", "rp"})
"""Buttons the crawl records but does not tap: they change state, start a flow or mint a login link."""

ZH_KEEPS = {"Demo", "Language", "English", "seed", "phrase", "Meta", "muse", "spark", "Pro", "PayPal"}
"""English the zh-Hant UI shows on purpose: the 🎬 Demo name, the "🌐 Language / 語言" picker with its 🇬🇧 English choice, the
「助記詞（seed phrase）」 gloss in the security note and the model name (Meta, muse-spark-1.3) in ❓ Help."""


def in_language(lang: str, text: str, markup) -> bool:
    parts = [text, *(b.text for b in kb.iter_buttons(markup))]
    if lang == EN:
        return not any(has_chinese(p) for p in parts)
    return not [w for p in parts for w in english_words(p) if w not in ZH_KEEPS]


def registered_commands(router) -> set[str]:
    found: set[str] = set()
    for handler in router.message.handlers:
        for f in handler.filters or ():
            if isinstance(f.callback, Command):
                found.update(c for c in f.callback.commands if isinstance(c, str))
    for sub in router.sub_routers:
        found |= registered_commands(sub)
    return found


@pytest.fixture
def app(ctx):
    return wire_bot(ctx, AsyncMock())


async def crawl(ui, uid: int, lang: str, limit: int = 300):
    """Tap every view button from 🏠 Main menu; returns {(action, args): label} of every button seen and the screens."""
    buttons: dict[tuple, str] = {}
    screens: dict[tuple, tuple] = {}
    queue = [("m", ())]
    while queue and len(screens) < limit:
        key = queue.pop(0)
        if key in screens:
            continue
        screen = await ui.router.run(uid, *key, T=Translator(lang))
        screens[key] = (screen.text, screen.keyboard)
        for b in kb.iter_buttons(screen.keyboard):
            if not b.callback_data:
                continue
            d = ui.codec.decode(uid, b.callback_data)
            buttons.setdefault((d.action, d.args), b.text)
            if d.action not in SIDE_EFFECTS and (d.action, d.args) not in screens:
                queue.append((d.action, d.args))
    return buttons, screens


class TestEveryCommandHasAButton:
    def test_every_registered_command_has_a_route(self, app):
        commands = registered_commands(app.dispatcher)
        assert set(BOT_COMMANDS) <= commands
        assert {"risk", "status", "actions", "killswitch", "threshold", "ask", "dashboard", "unbind"} <= commands
        assert not commands - set(MENU_ROUTES), f"commands with no inline-keyboard route: {sorted(commands - set(MENU_ROUTES))}"
        assert set(MENU_ROUTES.values()) <= set(ACTIONS)

    def test_bot_command_list_is_bilingual(self):
        for c in BOT_COMMANDS:
            assert Translator(EN)(f"commands.{c}") != f"commands.{c}" != Translator(ZH)(f"commands.{c}")

    @pytest.mark.parametrize("lang", LANGUAGES)
    async def test_every_route_is_a_button_reachable_from_the_main_menu(self, ctx, app, lang):
        uid = ALICE
        await ctx.storage.save_user(User(telegram_id=uid, threshold=ctx.settings.default_threshold(), language=lang,
                                         language_explicit=True, is_demo_mode=True))
        sim = ctx.simulations.get_or_create(uid)
        sim.tick_interval = 60
        await sim.start(source="test")
        try:
            buttons, screens = await crawl(app.ui, uid, lang)
        finally:
            await app.ui.services.stop_demo(uid)
        seen = {action for action, _ in buttons}
        missing = {cmd: action for cmd, action in MENU_ROUTES.items() if action not in seen}
        assert not missing, f"/{', /'.join(missing)} only reachable by typing"
        for key, (text, markup) in screens.items():
            assert in_language(lang, text, markup), (lang, key, text[:120])

    @pytest.mark.parametrize("lang", LANGUAGES)
    async def test_risk_overview_and_stop_check_are_labelled_on_the_main_menu(self, app, lang):
        T = Translator(lang)
        screen = await app.ui.router.run(ALICE, "m", T=T)
        rows = [[b.text for b in row] for row in screen.keyboard.inline_keyboard]
        assert rows[0] == [
            {"en": "📊 Risk overview", "zh-Hant": "📊 風險總覽"}[lang],
            {"en": "🤖 Muse checkout", "zh-Hant": "🤖 Muse 結帳"}[lang],
        ]
        assert {"en": "🛡 Stop-loss check", "zh-Hant": "🛡 停損檢查"}[lang] in rows[1]


class TestDemoPathToTheStopCheck:
    """Positions list → a position → 救倉 / 插針 / 停損檢查 / 資金費, and 🛡 Stop-loss check straight from the menu."""

    @pytest.mark.parametrize("lang", LANGUAGES)
    async def test_demo_book_positions_reach_every_tool(self, ctx, app, lang):
        await enable_demo(ctx, ALICE)
        await app.ui.services.set_language(ALICE, lang)
        T, ui = Translator(lang), app.ui
        listed = await ui.router.run(ALICE, "pl", T=T)
        actions = [(ui.codec.decode(ALICE, b.callback_data).action, ui.codec.decode(ALICE, b.callback_data).args)
                   for b in kb.iter_buttons(listed.keyboard)]
        positions = sorted({args for a, args in actions if a == "pd"})
        assert positions and all(source == "demo" for source, _ in positions)
        for args in positions:
            detail = await ui.router.run(ALICE, "pd", args, T=T)
            labels = [b.text for b in kb.iter_buttons(detail.keyboard)]
            assert [T(f"tools.name.{n}") for n in ("rescue", "wick", "stop", "funding")] == labels[:4]
            stop = await ui.router.run(ALICE, "ps", args, T=T)
            assert stop.text.startswith(T("tools.name.stop"))

        stops = await ui.router.run(ALICE, "sc", T=T)
        targets = {ui.codec.decode(ALICE, b.callback_data).args for b in kb.iter_buttons(stops.keyboard)
                   if ui.codec.decode(ALICE, b.callback_data).action == "ps"}
        assert targets == set(positions)
        assert stops.text.startswith(T("stopcheck.title", book=T("book.demo")))
        assert T("tools.name.stop") == {"en": "🛑 Stop-loss check", "zh-Hant": "🛑 停損檢查"}[lang]

    async def test_running_simulation_positions_are_listed_as_sim(self, ctx, app):
        await enable_demo(ctx, ALICE)
        sim = ctx.simulations.get_or_create(ALICE)
        sim.tick_interval = 60
        await sim.start(source="test")
        try:
            stops = await app.ui.router.run(ALICE, "sc")
            sources = {app.ui.codec.decode(ALICE, b.callback_data).args[0] for b in kb.iter_buttons(stops.keyboard)
                       if app.ui.codec.decode(ALICE, b.callback_data).action == "ps"}
            assert sources == {"sim"}
        finally:
            await app.ui.services.stop_demo(ALICE)


class TestNewScreens:
    @pytest.mark.parametrize("lang", LANGUAGES)
    async def test_risk_command_opens_the_overview_with_one_muse_line(self, ctx, app, lang):
        await enable_demo(ctx, ALICE)
        await app.ui.services.set_language(ALICE, lang)
        T = Translator(lang)
        message = fake_message(ALICE, "/risk")
        message.bot = AsyncMock()
        await app.ui.on_command(message, MagicMock(clear=AsyncMock()), MagicMock(command="risk", args=None))
        text, markup = message.answer.await_args.args[0], message.answer.await_args.kwargs["reply_markup"]
        assert text.startswith(T("overview.title", book=T("book.demo")))
        muse = [line for line in text.splitlines() if "✦ Muse" in line]
        assert len(muse) == 1 and muse[0].startswith("💡 ")
        assert in_language(lang, text, markup)
        first = [b for b in kb.iter_buttons(markup) if app.ui.codec.decode(ALICE, b.callback_data).action == "pr"]
        assert first and first[0].text.startswith("🚑")

    async def test_killswitch_command_and_button_toggle_the_same_switch(self, ctx, app):
        actions = ctx.actions
        message = fake_message(ALICE, "/killswitch")
        message.bot = AsyncMock()
        await app.ui.on_command(message, MagicMock(clear=AsyncMock()), MagicMock(command="killswitch", args=None))
        assert actions.is_kill_switch_active(ALICE)
        markup = message.answer.await_args.kwargs["reply_markup"]
        toggle = next(b for b in kb.iter_buttons(markup) if app.ui.codec.decode(ALICE, b.callback_data).action == "ak")
        assert app.ui.codec.decode(ALICE, toggle.callback_data).args == ("off",)
        await app.ui.router.run(ALICE, "ak", ("off",))
        assert not actions.is_kill_switch_active(ALICE)
        await app.ui.router.run(ALICE, "ak", ("off",))
        assert not actions.is_kill_switch_active(ALICE)

    @pytest.mark.parametrize("lang", LANGUAGES)
    async def test_pending_actions_confirm_and_cancel_from_buttons(self, ctx, app, lang, monkeypatch):
        action = MagicMock(action_id="pa_0123456789ab")
        action.advice.action_type.value = "add_margin"
        action.advice.symbol, action.advice.margin_to_add_usd = "BTCUSDT", 250.0
        action.status.value = "proposed"
        pending = [action]
        monkeypatch.setattr(ctx.actions, "get_pending_actions", lambda uid: list(pending) if uid == ALICE else [])
        monkeypatch.setattr(ctx.actions, "cancel_action", lambda aid, uid: (pending.clear() or True, "ok"))
        await app.ui.services.set_language(ALICE, lang)
        T = Translator(lang)
        screen = await app.ui.router.run(ALICE, "ac", T=T)
        assert T("account.pending_title") in screen.text and "BTCUSDT" in screen.text
        by_action = {app.ui.codec.decode(ALICE, b.callback_data).action: b for b in kb.iter_buttons(screen.keyboard)}
        assert {"ay", "an", "ak", "se", "b"} <= set(by_action)
        assert in_language(lang, screen.text, screen.keyboard)
        after = await app.ui.router.run(ALICE, "an", ("pa_0123456789ab",), T=T)
        assert after.toast == T("account.cancelled_toast") and T("account.pending_none") in after.text

    @pytest.mark.parametrize("lang", LANGUAGES)
    async def test_ask_muse_presets_answer_in_the_readers_language(self, ctx, app, lang):
        await enable_demo(ctx, ALICE)
        await app.ui.services.set_language(ALICE, lang)
        T = Translator(lang)
        menu = await app.ui.router.run(ALICE, "qa", T=T)
        presets = [app.ui.codec.decode(ALICE, b.callback_data) for b in kb.iter_buttons(menu.keyboard)]
        assert [d.args for d in presets if d.action == "qq"] == [("risk",), ("first",), ("pnl",)]
        for (key,) in [d.args for d in presets if d.action == "qq"]:
            answer = await app.ui.router.run(ALICE, "qq", (key,), T=T)
            assert answer.text.startswith(T("ask.answer", question=T(f"ask.q.{key}")))
            assert answer.text.rstrip().endswith(T("alert.muse_mark_mock"))
            assert in_language(lang, answer.text, answer.keyboard)
