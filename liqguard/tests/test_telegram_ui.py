"""
Inline-keyboard UI in both languages (English default, 繁體中文): callback
codec, keyboard builders, router dispatch, user scoping, answerCallbackQuery,
stale buttons, an end-to-end demo click-through, the guided binding flow,
alert buttons that always match their message's language, and the demo's
mute / stop guarantees (nothing is sent after ✅ I'm awake or ⏹ Stop demo).
"""

import ast
import asyncio
import logging
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from liqguard.core.binding import BindingService, security_note
from liqguard.exchanges.base import KeyCheck
from liqguard.i18n import EN, LANGUAGES, ZH, Translator
from liqguard.telegram import BotHandlers, TelegramNotifier
from liqguard.telegram.sim_bridge import TelegramSimulationBridge
from liqguard.telegram.ui import BOT_COMMANDS, TelegramUI, bot_commands
from liqguard.telegram.ui import keyboards as kb
from liqguard.telegram.ui.actions import ACTIONS
from liqguard.telegram.ui.codec import MAX_BYTES, CallbackCodec, CallbackError, is_ui_payload
from liqguard.telegram.ui.features import FEATURES, dispatch_table
from liqguard.telegram.ui.features.binding import BindStates
from liqguard.telegram.ui.router import ERROR, EXPIRED, EXPIRED_TOAST, INVALID, CallbackRouter
from liqguard.telegram.ui.screen import Signer
from liqguard.telegram.ui.services import UIServices

from .conftest import ALICE, BOB, fake_message, until

UI_DIR = Path(__file__).resolve().parents[1] / "telegram" / "ui"
API_KEY = "Kq7" + "x" * 50 + "AB12"
API_SECRET = "Sz9" + "y" * 50 + "CD34"
WALLET = "0x" + "ab" * 20
CJK = re.compile(r"[\u4e00-\u9fff]")
EN_ALERT_WORDS = ("ALERT**", "REMINDER", "STOP-LOSS", "POSITIONS AT RISK", "PRE-FUNDING", "ADL WARNING",
                  "POSITION SIZE", "STRESS TEST", "Muse (mock)", " · demo")

SAMPLE_ARGS = {
    "m": (), "h": (), "l": (), "pl": (), "tr": (), "st": (), "ff": (), "se": (), "b": (), "bb": (), "bh": (), "bl": (),
    "bu": (), "bc": (), "db": (), "dm": (), "dx": (), "do": (), "ls": ("zh",),
    "pd": ("sim", "BTCUSDT"), "pr": ("binance", "ETHUSDT"), "pw": ("hyperliquid", "kPEPE"), "ps": ("demo", "SOLUSDT"),
    "pf": ("sim", "AVAXUSDT"), "sp": ("quiet",), "ss": ("quiet", "23-7"), "bx": ("binance", "mainnet"),
    "by": ("hyperliquid", "mainnet"), "ds": ("cross_longs",), "aa": ("AbCdEf123_-x",), "az": ("AbCdEf123_-x",),
}


# --------------------------------------------------------------------------- fixtures & helpers


class FakeAdapter:
    """Stands in for BinanceAdapter / HyperliquidAdapter; `check` is set per test."""

    check = KeyCheck(True, "Read-only key accepted", ["READ"], True)
    seen: list = []

    def __init__(self, *args, **kwargs):
        FakeAdapter.seen.append(args)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def check_key(self):
        return FakeAdapter.check


@pytest.fixture
def adapter():
    FakeAdapter.check = KeyCheck(True, "Read-only key accepted", ["READ"], True)
    FakeAdapter.seen = []
    return FakeAdapter


@pytest.fixture
async def ui(ctx, adapter):
    binding = BindingService(ctx.storage, binance_factory=adapter, hyperliquid_factory=adapter,
                             default_threshold=ctx.settings.default_threshold)
    return TelegramUI(ctx, services=UIServices(ctx, binding))


@pytest.fixture
def fsm_storage():
    return MemoryStorage()


@pytest.fixture(params=LANGUAGES)
async def lang(request, ui):
    """Run the test once per language, with ALICE having picked it."""
    await ui.services.set_language(ALICE, request.param)
    return request.param


def state_for(storage, uid: int) -> FSMContext:
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def make_callback(uid: int, data, chat_type: str = "private", language_code=None):
    cb = AsyncMock()
    cb.from_user = MagicMock(id=uid, language_code=language_code)
    cb.data = data
    cb.message = AsyncMock()
    cb.message.chat = MagicMock(type=chat_type, id=uid)
    cb.bot = AsyncMock()
    return cb


async def click(ui, uid, data, state=None, chat_type="private", language_code=None):
    cb = make_callback(uid, data, chat_type, language_code)
    handled = await ui.router.handle(cb, state)
    return cb, handled


def shown(cb):
    """(text, markup) of what the tap rendered: an in-place edit, else a new message."""
    for call in (cb.message.edit_text.await_args, cb.message.answer.await_args):
        if call is not None:
            return call.args[0], call.kwargs.get("reply_markup")
    call = cb.bot.send_message.await_args
    if call is not None:
        return call.args[1], call.kwargs.get("reply_markup")
    return None, None


def toast(cb):
    call = cb.answer.await_args
    return call.args[0] if call.args else None, call.kwargs.get("show_alert")


def actions_of(ui, uid, markup) -> list[tuple[str, tuple]]:
    out = []
    for b in kb.iter_buttons(markup):
        if b.callback_data:
            d = ui.codec.decode(uid, b.callback_data)
            out.append((d.action, d.args))
    return out


def labels(markup) -> list[str]:
    return [b.text for b in kb.iter_buttons(markup)]


def all_keyboards(T: Translator, sign: Signer) -> list:
    """Every keyboard builder with representative data (the source of every button the bot can render)."""
    settings = {"warning": 20, "critical": 10, "emergency": 5, "interval": 2, "max": 5, "quiet_label": T("settings.quiet_off"),
                "escalation": True, "adaptive": False, "stops": True}
    return [
        kb.main_menu(T, sign),
        kb.main_menu(T, sign, demo_running=True),
        kb.language_menu(T, sign),
        kb.positions_list(T, sign, [("sim", "BTCUSDT", "🆘 BTCUSDT · 3.0%"), ("binance", "ETHUSDT", "⚠️ ETHUSDT · 12.0%")]),
        kb.position_tools(T, sign, "sim", "BTCUSDT"),
        kb.position_tools(T, sign, "sim", "BTCUSDT", current="pr"),
        kb.triage(T, sign, [("sim", "BTCUSDT", "BTCUSDT #1")]),
        kb.stress(T, sign, [("sim", "BTCUSDT")]),
        kb.refresh_back(T, sign, "ff"),
        kb.no_account(T, sign),
        kb.back_only(T, sign),
        kb.settings(T, sign, settings),
        *[kb.setting_options(T, sign, key, values, values[0]) for key, values in
          {"warning": (15, 20), "critical": (8, 10), "emergency": (3, 5), "interval": (1, 2), "max": (3, 5),
           "quiet": ("off", "23-7")}.items()],
        kb.binding_menu(T, sign),
        kb.binding_waiting(T, sign),
        kb.binding_back(T, sign),
        kb.unbind_list(T, sign, [("binance", "mainnet", "🟡 Binance …AB12"), ("hyperliquid", "mainnet", "🟢 Hyperliquid 0xabab…abab")]),
        kb.unbind_confirm(T, sign, "binance", "mainnet"),
        kb.dashboard(T, sign, "https://liqguard.test/auth/login?token=x"),
        kb.demo_menu(T, sign, running=False, demo_mode=True),
        kb.demo_menu(T, sign, running=True, demo_mode=True),
        kb.demo_started(T, sign),
        kb.alert_keyboard(T, sign, "sim", "BTCUSDT", "AbCdEf123_-x"),
        kb.alert_keyboard(T, sign, "sim", "BTCUSDT", "AbCdEf123_-x", demo=True),
    ]


def has_chinese(text: str) -> bool:
    """CJK outside the language names that every language shows ("🌐 Language / 語言", "繁體中文")."""
    return bool(CJK.search(text.replace("語言", "").replace("繁體中文", "")))


def in_language(lang: str, text: str, markup) -> bool:
    """Body and every button label written in `lang` (no CJK in English, no English alert heads or labels in 繁體中文)."""
    names = labels(markup)
    if lang == EN:
        return not CJK.search(text) and not any(CJK.search(n) for n in names)
    return not any(w in text for w in EN_ALERT_WORDS) and all(CJK.search(n) for n in names)


async def enable_demo(ctx, uid: int, scenario: str = "critical") -> None:
    from liqguard.models import User

    if await ctx.storage.get_user(uid) is None:
        await ctx.storage.save_user(User(telegram_id=uid, threshold=ctx.settings.default_threshold()))
    await ctx.storage.set_demo_mode(uid, True)
    ctx.positions.demo_adapter(uid, scenario)


# --------------------------------------------------------------------------- 1. codec


class TestCodec:
    codec = CallbackCodec(b"k" * 32)

    def test_sample_args_cover_every_action(self):
        assert set(SAMPLE_ARGS) == set(ACTIONS)

    @pytest.mark.parametrize("action", sorted(ACTIONS))
    def test_roundtrip_every_action(self, action):
        args = SAMPLE_ARGS[action]
        data = self.codec.encode(ALICE, action, *args, now=1_800_000_000)
        assert len(data.encode()) <= MAX_BYTES
        assert ACTIONS[action].valid_args(args)
        decoded = self.codec.decode(ALICE, data, now=1_800_000_000)
        assert (decoded.action, decoded.args, decoded.issued_at) == (action, args, 1_800_000_000)

    def test_longest_real_payloads_fit_in_64_bytes(self):
        for action, args in [("pd", ("hyperliquid", "Z" * 20)), ("aa", ("a" * 24,)), ("ds", ("cross_longs",)),
                             ("by", ("hyperliquid", "testnet")), ("ss", ("emergency", "toggle"))]:
            assert len(self.codec.encode(ALICE, action, *args, now=4_000_000_000).encode()) <= MAX_BYTES

    @pytest.mark.parametrize("data", [
        None, "", "1", "1.", "1.m", "1..0.AAAAAAAA", "1.M.0.AAAAAAAA", "1.m.!!.0.AAAAAAAA", "1.m.0.short",
        "1.m..0.AAAAAAAA", "rescue:sim:BTCUSDT", "ack:abc", "1.m.0.AAAA AAA", "1.mü.0.AAAAAAAA", 12345,
    ])
    def test_malformed_payloads_rejected(self, data):
        with pytest.raises(CallbackError) as e:
            self.codec.decode(ALICE, data)
        assert e.value.reason in ("format", "version")

    def test_unknown_version_rejected(self):
        data = self.codec.encode(ALICE, "m")
        with pytest.raises(CallbackError) as e:
            self.codec.decode(ALICE, "2" + data[1:])
        assert e.value.reason == "version"

    def test_tampered_argument_rejected(self):
        data = self.codec.encode(ALICE, "pr", "sim", "BTCUSDT")
        with pytest.raises(CallbackError) as e:
            self.codec.decode(ALICE, data.replace("BTCUSDT", "ETHUSDT"))
        assert e.value.reason == "tag"

    def test_tampered_action_and_timestamp_rejected(self):
        data = self.codec.encode(ALICE, "bx", "binance", "mainnet")
        for forged in (data.replace("1.bx.", "1.by.", 1), self._bump_ts(data)):
            with pytest.raises(CallbackError) as e:
                self.codec.decode(ALICE, forged)
            assert e.value.reason == "tag"

    def test_flipped_tag_rejected(self):
        data = self.codec.encode(ALICE, "m")
        flipped = data[:-1] + ("A" if data[-1] != "A" else "B")
        with pytest.raises(CallbackError) as e:
            self.codec.decode(ALICE, flipped)
        assert e.value.reason == "tag"

    def test_other_secret_rejected(self):
        data = CallbackCodec(b"other" * 8).encode(ALICE, "m")
        with pytest.raises(CallbackError):
            self.codec.decode(ALICE, data)

    def test_oversized_payload_rejected_before_parsing(self):
        data = self.codec.encode(ALICE, "m")
        for big in (data + "A" * 70, "1." + "ü" * 40):
            assert len(big.encode()) > MAX_BYTES
            with pytest.raises(CallbackError) as e:
                self.codec.decode(ALICE, big)
            assert e.value.reason == "size"

    def test_encode_refuses_oversized_or_unsafe_payloads(self):
        with pytest.raises(ValueError):
            self.codec.encode(ALICE, "pd", "A" * 24, "B" * 24, "C" * 24)
        for bad in ("a.b", "x:y", "", "A" * 25, "空"):
            with pytest.raises(ValueError):
                self.codec.encode(ALICE, "pd", "sim", bad)
        with pytest.raises(ValueError):
            self.codec.encode(ALICE, "Bad.Action")

    def test_future_timestamp_rejected(self):
        data = self.codec.encode(ALICE, "m", now=time.time() + 3600)
        with pytest.raises(CallbackError):
            self.codec.decode(ALICE, data)

    def test_max_age_marks_old_buttons_expired(self):
        data = self.codec.encode(ALICE, "m", now=1_000)
        with pytest.raises(CallbackError) as e:
            self.codec.decode(ALICE, data, max_age=60, now=2_000)
        assert e.value.reason == "expired"

    def test_only_versioned_payloads_belong_to_the_ui(self):
        assert is_ui_payload(self.codec.encode(ALICE, "m"))
        for legacy in ("rescue:sim:BTCUSDT", "ack:abc", "settings:max:5", "venue:binance", None, ""):
            assert not is_ui_payload(legacy)

    @staticmethod
    def _bump_ts(data: str) -> str:
        parts = data.split(".")
        parts[-2] = "1" + parts[-2][1:] if parts[-2][0] != "1" else "2" + parts[-2][1:]
        return ".".join(parts)


# --------------------------------------------------------------------------- 2. keyboards (pure builders)


class TestKeyboards:
    sign = Signer(CallbackCodec(b"k" * 32), ALICE, now=1_800_000_000)

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_main_menu_has_every_feature_and_the_language_button(self, lang):
        T = Translator(lang)
        keys = ["positions", "triage", "stress", "funding", "settings", "binding", "dashboard", "demo", "language", "help"]
        assert labels(kb.main_menu(T, self.sign)) == [T(f"kb.{k}") for k in keys]
        assert labels(kb.main_menu(T, self.sign, demo_running=True))[0] == T("kb.sim_stop")
        assert T("kb.language") == "🌐 Language / 語言"

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_every_position_gets_its_four_tools_and_a_way_back(self, lang):
        T = Translator(lang)
        markup = kb.positions_list(T, self.sign, [("sim", "BTCUSDT", "BTC"), ("sim", "ETHUSDT", "ETH")])
        tools = [T(f"tools.name.{n}") for n in ("rescue", "wick", "stop", "funding")]
        assert labels(markup) == ["BTC", *tools, "ETH", *tools, T("kb.refresh"), T("kb.back_menu")]
        acts = [a for a, _ in actions_of_codec(self.sign, markup)]
        assert acts[:5] == ["pd", "pr", "pw", "ps", "pf"]

    def test_tool_screen_hides_the_current_tool(self):
        T = Translator(ZH)
        names = labels(kb.position_tools(T, self.sign, "sim", "BTCUSDT", current="pw"))
        assert T("tools.name.wick") not in names and T("tools.name.rescue") in names and names[-1] == T("kb.back_menu")

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_alert_buttons(self, lang):
        T = Translator(lang)
        assert labels(kb.alert_keyboard(T, self.sign, "sim", "BTCUSDT", "abcdef123")) == [
            T("kb.alert_rescue"), T("kb.alert_awake"), T("kb.alert_snooze")]
        assert labels(kb.alert_keyboard(T, self.sign, "sim", "BTCUSDT", "abcdef123", demo=True)) == [
            T("kb.alert_rescue"), T("kb.alert_awake"), T("kb.alert_snooze_demo"), T("kb.sim_stop")]
        assert labels(kb.alert_keyboard(T, self.sign, "sim", "BTCUSDT", None)) == [T("kb.alert_rescue")]
        assert labels(kb.alert_keyboard(T, self.sign, None, None, None, demo=True)) == [T("kb.sim_stop")]
        assert kb.alert_keyboard(T, self.sign, None, None, None) is None

    def test_stop_demo_label(self):
        assert Translator(EN)("kb.sim_stop") == "⏹ Stop demo" and Translator(ZH)("kb.sim_stop") == "⏹ 停止 Demo"

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_builders_are_pure(self, lang):
        T = Translator(lang)
        assert [m.model_dump() for m in all_keyboards(T, self.sign)] == [m.model_dump() for m in all_keyboards(T, self.sign)]

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_every_button_fits_and_is_signed_for_its_user(self, lang):
        codec = self.sign.codec
        for markup in all_keyboards(Translator(lang), self.sign):
            for b in kb.iter_buttons(markup):
                if b.url:
                    continue
                assert len(b.callback_data.encode()) <= MAX_BYTES
                codec.decode(ALICE, b.callback_data, now=1_800_000_000)
                with pytest.raises(CallbackError):
                    codec.decode(BOB, b.callback_data, now=1_800_000_000)

    def test_both_languages_render_the_same_buttons(self):
        en = [actions_of_codec(self.sign, m) for m in all_keyboards(Translator(EN), self.sign)]
        zh = [actions_of_codec(self.sign, m) for m in all_keyboards(Translator(ZH), self.sign)]
        assert en == zh

    def test_dashboard_url_button_only_for_https(self):
        T = Translator(EN)
        assert labels(kb.dashboard(T, self.sign, "http://localhost/x")) == [T("kb.back_menu")]
        assert labels(kb.dashboard(T, self.sign, "https://x.test/y"))[0] == T("kb.open_dashboard")

    def test_chinese_labels_are_traditional_and_english_labels_have_no_chinese(self):
        simplified = set("险仓设绑帐户启关闭页种发误确认损费详")
        for markup in all_keyboards(Translator(ZH), self.sign):
            for name in labels(markup):
                assert not simplified & set(name), name
        for markup in all_keyboards(Translator(EN), self.sign):
            for name in labels(markup):
                assert not CJK.search(name) or name in ("🌐 Language / 語言", "🇹🇼 繁體中文"), name


def actions_of_codec(sign: Signer, markup) -> list[tuple[str, tuple]]:
    out = []
    for b in kb.iter_buttons(markup):
        if b.callback_data:
            d = sign.codec.decode(sign.user_id, b.callback_data, now=sign.now)
            out.append((d.action, d.args))
    return out


# --------------------------------------------------------------------------- 3. modularity


def _imports(path: Path) -> list[tuple[int, str, list[str]]]:
    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            out.append((node.level, node.module or "", [a.name for a in node.names]))
        elif isinstance(node, ast.Import):
            out.extend((0, a.name, []) for a in node.names)
    return out


class TestModularity:
    def test_features_never_import_each_other(self):
        features = {p.stem for p in (UI_DIR / "features").glob("*.py")} - {"__init__"}
        assert features == set(FEATURES)
        for name in features:
            for level, module, names in _imports(UI_DIR / "features" / f"{name}.py"):
                if level == 1:
                    pytest.fail(f"features/{name}.py imports sibling .{module}")
                if level == 2:
                    targets = [module] if module else names
                    assert set(targets) <= {"keyboards", "screen", "texts"}, (name, targets)
                if level == 0:
                    assert "telegram.ui.features" not in module and "telegram.handlers" not in module, (name, module)

    def test_keyboards_are_pure_builders(self):
        for level, module, names in _imports(UI_DIR / "keyboards.py"):
            if level:
                assert module == "screen", module
            else:
                assert module.split(".")[0] in ("__future__", "typing", "aiogram") \
                    or module in ("liqguard.i18n", "liqguard.core.user_settings"), module

    def test_codec_has_no_framework_dependencies(self):
        assert all(module.split(".")[0] not in ("aiogram", "liqguard") for _, module, _ in _imports(UI_DIR / "codec.py"))

    def test_dispatch_table_matches_action_registry(self):
        table = dispatch_table()
        assert set(table) == set(ACTIONS)
        for action, handler in table.items():
            assert handler.__module__ == f"liqguard.telegram.ui.features.{ACTIONS[action].feature}", action

    def test_router_refuses_an_incomplete_or_unknown_table(self):
        table = dispatch_table()
        codec = CallbackCodec(b"k" * 32)
        with pytest.raises(RuntimeError):
            CallbackRouter(codec, None, {k: v for k, v in table.items() if k != "pl"})
        with pytest.raises(RuntimeError):
            CallbackRouter(codec, None, {**table, "zz": table["m"]})


# --------------------------------------------------------------------------- 4. router dispatch


class TestRouterDispatch:
    def test_every_action_is_reachable_from_some_keyboard(self, ui):
        sign = ui.signer(ALICE)
        for lang in LANGUAGES:
            reachable = {a for m in all_keyboards(Translator(lang), sign) for a, _ in actions_of(ui, ALICE, m)}
            assert reachable == set(ACTIONS), lang

    async def test_every_button_routes_to_its_handler_in_both_languages(self, ctx, ui, fsm_storage, lang):
        await enable_demo(ctx, ALICE)
        T = Translator(lang)
        sign = ui.signer(ALICE)
        checked = 0
        for markup in all_keyboards(T, sign):
            for b in kb.iter_buttons(markup):
                if not b.callback_data:
                    continue
                expected = ui.codec.decode(ALICE, b.callback_data).action
                if expected == "ls":
                    continue
                cb, handled = await click(ui, ALICE, b.callback_data, state_for(fsm_storage, ALICE))
                assert handled == expected, (b.text, handled, expected)
                assert toast(cb)[0] != T(ERROR), b.text
                assert cb.answer.await_count == 1
                text, _ = shown(cb)
                shown_text = text or toast(cb)[0]
                assert shown_text and has_chinese(shown_text) == (lang == ZH), (lang, b.text, shown_text[:80])
                checked += 1
        assert checked > 60

    async def test_language_button_switches_and_is_saved(self, ctx, ui):
        cb, handled = await click(ui, ALICE, ui.signer(ALICE)("ls", "zh"))
        assert handled == "ls" and toast(cb)[0] == Translator(ZH)("lang.set_toast")
        user = await ctx.storage.get_user(ALICE)
        assert user.language == ZH and user.language_explicit
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("m"), language_code="en")
        assert shown(cb)[0].startswith(Translator(ZH)("menu.text", status="").split("\n")[0])

    async def test_telegram_language_picks_the_default(self, ui):
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("m"), language_code="zh-TW")
        assert shown(cb)[0].startswith("🏠 主選單")
        cb, _ = await click(ui, BOB, ui.signer(BOB)("m"), language_code="de")
        assert shown(cb)[0].startswith("🏠 Main menu")

    async def test_legacy_callbacks_are_left_to_bot_handlers(self, ctx, ui):
        dp = Dispatcher(storage=MemoryStorage())
        ui.register(dp)
        BotHandlers(ctx).register(dp, AsyncMock())
        assert dp.sub_routers[0] is ui.aiogram_router
        assert not is_ui_payload("rescue:sim:BTCUSDT")

    async def test_set_my_commands_for_default_and_zh(self, ui):
        bot = AsyncMock()
        await ui.set_commands(bot)
        calls = bot.set_my_commands.await_args_list
        assert [c.kwargs.get("language_code") for c in calls] == [None, "zh"]
        for c, lang in zip(calls, LANGUAGES):
            commands = c.args[0]
            assert [x.command for x in commands] == list(BOT_COMMANDS)
            assert [x.description for x in commands] == [x.description for x in bot_commands(lang)]
            assert all(x.command.islower() and len(x.command) <= 32 and x.description for x in commands)
        assert "stopdemo" in BOT_COMMANDS and "lang" in BOT_COMMANDS
        bot.set_my_commands.side_effect = RuntimeError("telegram down")
        await ui.set_commands(bot)

    @pytest.mark.parametrize("text,command,action", [
        ("/start", "start", "m"), ("/menu", "menu", "m"), ("/help", "help", "h"), ("/lang", "lang", "l"),
        ("/positions", "positions", "pl"), ("/bind", "bind", "b"), ("/demo", "demo", "dm"), ("/settings", "settings", "se"),
        ("/stopdemo", "stopdemo", "dx"),
    ])
    async def test_commands_open_their_screen(self, ui, fsm_storage, text, command, action):
        message = fake_message(ALICE, text)
        message.bot = AsyncMock()
        state = state_for(fsm_storage, ALICE)
        await state.set_state(BindStates.binance_key)
        await ui.on_command(message, state, MagicMock(command=command, args=None))
        body, markup = message.answer.await_args.args[0], message.answer.await_args.kwargs["reply_markup"]
        expected = await ui.router.run(ALICE, action, state=state_for(fsm_storage, ALICE))
        assert body.split("\n")[0] == expected.text.split("\n")[0]
        assert markup is not None
        assert await state.get_state() is None

    async def test_start_shows_main_menu(self, ui, fsm_storage, lang):
        T = Translator(lang)
        message = fake_message(ALICE, "/start")
        message.bot = AsyncMock()
        await ui.on_command(message, state_for(fsm_storage, ALICE), MagicMock(command="start", args=None))
        text, markup = message.answer.await_args.args[0], message.answer.await_args.kwargs["reply_markup"]
        assert text.split("\n")[0] == T("menu.text", status="").split("\n")[0]
        assert len(labels(markup)) == 10 and T("kb.language") in labels(markup)

    async def test_settings_with_arguments_falls_through_to_legacy(self, ui, fsm_storage):
        with pytest.raises(SkipHandler):
            await ui.on_command(fake_message(ALICE, "/settings max 5"), state_for(fsm_storage, ALICE),
                                MagicMock(command="settings", args="max 5"))


# --------------------------------------------------------------------------- 5. user scoping


class TestUserScoping:
    async def test_user_b_cannot_press_user_a_button(self, ctx, ui):
        await enable_demo(ctx, ALICE)
        data = ui.signer(ALICE)("pl")
        cb, handled = await click(ui, BOB, data, chat_type="group")
        assert handled is None
        assert toast(cb) == (Translator(EN)(INVALID), True)
        cb.message.edit_text.assert_not_awaited()
        cb.message.answer.assert_not_awaited()

    async def test_buttons_differ_per_user(self, ui):
        assert ui.signer(ALICE)("pr", "sim", "BTCUSDT") != ui.signer(BOB)("pr", "sim", "BTCUSDT")

    async def test_b_signing_a_position_reference_only_sees_its_own_book(self, ctx, ui):
        await enable_demo(ctx, ALICE)
        for action in ("pd", "pr", "pw", "ps", "pf"):
            cb, handled = await click(ui, BOB, ui.signer(BOB)(action, "demo", "BTCUSDT"))
            text, _ = shown(cb)
            assert handled == action and text == Translator(EN)("positions.not_found")
        cb, _ = await click(ui, BOB, ui.signer(BOB)("pl"))
        assert "BTCUSDT" not in shown(cb)[0]

    async def test_b_cannot_ack_or_snooze_a_alert(self, ctx, ui):
        T = Translator(EN)
        ack = ctx.escalations.open(ALICE, "binance", "BTCUSDT", "critical", "x", time.time(), 60, 5)
        for action in ("aa", "az"):
            cb, _ = await click(ui, BOB, ui.signer(BOB)(action, ack.id))
            assert toast(cb)[0] == T("alerts.gone")
        assert ack.snoozed_until is None and ack.acknowledged_at is None
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("az", ack.id))
        assert toast(cb)[0] == T("alerts.snoozed_toast", minutes=30) and ack.snoozed_until >= time.time() + 29 * 60
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("aa", ack.id))
        assert ack.acknowledged_at is not None and toast(cb)[0] == T("alerts.acked_toast")

    async def test_b_cannot_unbind_a(self, ctx, ui):
        assert (await ui.services.binding.bind_hyperliquid(ALICE, WALLET)).ok
        cb, _ = await click(ui, BOB, ui.signer(BOB)("by", "hyperliquid", "mainnet"))
        assert shown(cb)[0] == Translator(EN)("binding.gone")
        assert len(await ui.services.binding.list(ALICE)) == 1

    async def test_settings_change_only_the_callers_thresholds(self, ctx, ui):
        await click(ui, ALICE, ui.signer(ALICE)("ss", "warning", "30"))
        assert (await ctx.storage.get_user(ALICE)).threshold.warning_distance_pct == 30
        bob = await ui.services.user(BOB)
        assert bob.threshold.warning_distance_pct == ctx.settings.default_threshold().warning_distance_pct

    async def test_language_is_per_user(self, ctx, ui):
        await click(ui, ALICE, ui.signer(ALICE)("ls", "zh"))
        assert (await ctx.storage.get_user(ALICE)).language == ZH
        cb, _ = await click(ui, BOB, ui.signer(BOB)("m"))
        assert shown(cb)[0].startswith("🏠 Main menu")

    async def test_demo_simulation_is_private(self, ctx, ui):
        await click(ui, ALICE, ui.signer(ALICE)("ds", "cross_longs"))
        assert ctx.simulations.get(ALICE) is not None and ctx.simulations.get(BOB) is None
        cb, _ = await click(ui, BOB, ui.signer(BOB)("pl"))
        assert shown(cb)[0].startswith("🔌")
        await ctx.simulations.get(ALICE).stop()


# --------------------------------------------------------------------------- 6. answerCallbackQuery always


class TestAlwaysAnswered:
    async def test_success_invalid_and_none(self, ui):
        for data in (ui.signer(ALICE)("m"), "1.m.0.AAAAAAAA", None, "", "x" * 200):
            cb, _ = await click(ui, ALICE, data)
            assert cb.answer.await_count == 1

    async def test_handler_exception(self, ui, monkeypatch, lang):
        async def boom(req):
            raise RuntimeError("db down")

        monkeypatch.setitem(ui.router.handlers, "pl", boom)
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("pl"))
        assert cb.answer.await_count == 1
        assert toast(cb) == (Translator(lang)(ERROR), True)

    async def test_render_failure(self, ui):
        cb = make_callback(ALICE, ui.signer(ALICE)("m"))
        cb.message.edit_text.side_effect = RuntimeError("message can't be edited")
        cb.message.answer.side_effect = RuntimeError("chat not found")
        cb.bot.send_message.side_effect = RuntimeError("blocked by user")
        await ui.router.handle(cb)
        assert cb.answer.await_count == 1
        assert toast(cb) == (Translator(EN)(ERROR), True)

    async def test_answer_failure_does_not_propagate(self, ui):
        cb = make_callback(ALICE, ui.signer(ALICE)("m"))
        cb.answer.side_effect = RuntimeError("query is too old")
        await ui.router.handle(cb)
        assert cb.answer.await_count == 1

    async def test_services_failure(self, ui, monkeypatch):
        monkeypatch.setattr(ui.services, "user", AsyncMock(side_effect=RuntimeError("storage locked")))
        monkeypatch.setattr(ui.services.binding, "list", AsyncMock(side_effect=RuntimeError("storage locked")))
        for action in ("m", "pl", "se", "b", "bl", "dm"):
            cb, _ = await click(ui, ALICE, ui.signer(ALICE)(action))
            assert cb.answer.await_count == 1 and toast(cb)[0] == Translator(EN)(ERROR)


# --------------------------------------------------------------------------- 7. stale / expired buttons


class TestStaleButtons:
    async def test_expired_button_opens_a_fresh_main_menu(self, ctx, ui, lang):
        T = Translator(lang)
        await enable_demo(ctx, ALICE)
        old = ui.codec.encode(ALICE, "pr", "demo", "BTCUSDT", now=time.time() - 3 * 86400)
        cb, handled = await click(ui, ALICE, old)
        text, markup = shown(cb)
        assert handled == "m" and text.startswith(T(EXPIRED))
        assert toast(cb)[0] == T(EXPIRED_TOAST)
        assert labels(markup)[0] == T("kb.positions")

    async def test_expired_unbind_confirmation_does_nothing(self, ui):
        assert (await ui.services.binding.bind_hyperliquid(ALICE, WALLET)).ok
        old = ui.codec.encode(ALICE, "by", "hyperliquid", "mainnet", now=time.time() - 180)
        cb, handled = await click(ui, ALICE, old)
        assert handled == "m"
        assert len(await ui.services.binding.list(ALICE)) == 1

    async def test_closed_position(self, ctx, ui, lang):
        T = Translator(lang)
        await enable_demo(ctx, ALICE)
        cb, handled = await click(ui, ALICE, ui.signer(ALICE)("pw", "demo", "NOPEUSDT"))
        assert handled == "pw" and shown(cb)[0] == T("positions.not_found") and toast(cb)[0] == T("positions.not_found_toast")

    async def test_simulation_buttons_after_demo_is_disabled(self, ctx, ui):
        await click(ui, ALICE, ui.signer(ALICE)("ds", "cross_longs"))
        stale = ui.signer(ALICE)("pr", "sim", "BTCUSDT")
        await click(ui, ALICE, ui.signer(ALICE)("do"))
        cb, _ = await click(ui, ALICE, stale)
        assert shown(cb)[0] == Translator(EN)("positions.not_found")

    async def test_already_acknowledged_alert(self, ctx, ui):
        ack = ctx.escalations.open(ALICE, "binance", "BTCUSDT", "critical", "x", time.time(), 60, 5)
        await click(ui, ALICE, ui.signer(ALICE)("aa", ack.id))
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("aa", ack.id))
        assert toast(cb)[0] == Translator(EN)("alerts.gone")
        cb.message.edit_text.assert_not_awaited()

    async def test_unchanged_screen_is_not_resent(self, ui):
        cb = make_callback(ALICE, ui.signer(ALICE)("h"))
        cb.message.edit_text.side_effect = RuntimeError("Bad Request: message is not modified")
        await ui.router.handle(cb)
        cb.message.answer.assert_not_awaited()
        assert toast(cb) == (None, False)

    async def test_inaccessible_message_falls_back_to_a_new_message(self, ui):
        cb = make_callback(ALICE, ui.signer(ALICE)("h"))
        cb.message = None
        await ui.router.handle(cb)
        cb.bot.send_message.assert_awaited_once()
        assert cb.bot.send_message.await_args.args[0] == ALICE

    async def test_button_from_before_a_secret_rotation(self, ui):
        old = CallbackCodec(b"previous-secret" * 3).encode(ALICE, "pl")
        cb, handled = await click(ui, ALICE, old)
        assert handled is None and toast(cb) == (Translator(EN)(INVALID), True)


# --------------------------------------------------------------------------- 8. end-to-end demo click-through


async def crawl(ui, uid, start_markup, state, skip=frozenset({"do", "dx", "by", "bb", "bh", "db", "ls"}), limit=400):
    """Breadth-first tap of every distinct button reachable from `start_markup`."""
    seen: set[tuple] = set()
    visited: set[str] = set()
    queue = [b.callback_data for b in kb.iter_buttons(start_markup)]
    while queue and len(seen) < limit:
        data = queue.pop(0)
        if data is None:
            continue
        d = ui.codec.decode(uid, data)
        if (d.action, d.args) in seen:
            continue
        seen.add((d.action, d.args))
        cb, handled = await click(ui, uid, data, state)
        assert handled == d.action
        assert toast(cb)[0] not in (Translator(EN)(ERROR), Translator(ZH)(ERROR)), (d.action, d.args)
        assert cb.answer.await_count == 1
        text, markup = shown(cb)
        assert text, (d.action, d.args)
        visited.add(handled)
        for b in kb.iter_buttons(markup):
            if b.callback_data and ui.codec.decode(uid, b.callback_data).action not in skip:
                queue.append(b.callback_data)
    return visited, seen


class TestDemoClickThrough:
    async def test_every_menu_path_in_demo_mode(self, ctx, ui, fsm_storage, lang):
        T = Translator(lang)
        state = state_for(fsm_storage, ALICE)
        start = fake_message(ALICE, "/start")
        start.bot = AsyncMock()
        await ui.on_command(start, state, MagicMock(command="start", args=None))
        menu = start.answer.await_args.kwargs["reply_markup"]

        await click(ui, ALICE, ui.signer(ALICE)("ds", "critical"), state)
        visited, seen = await crawl(ui, ALICE, menu, state)
        expected = set(ACTIONS) - {"do", "dx", "by", "bb", "bh", "bc", "db", "bx", "aa", "az", "ls"}
        assert expected <= visited, expected - visited
        tools = {(a, args) for a, args in seen if a in ("pr", "pw", "ps", "pf")}
        assert {args for _, args in tools} >= {("demo", s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")}

        for action in ("db", "bb", "bh", "bc"):
            cb, handled = await click(ui, ALICE, ui.signer(ALICE)(action), state)
            assert handled == action and toast(cb)[0] != T(ERROR)

        cb, handled = await click(ui, ALICE, ui.signer(ALICE)("dx"), state)
        assert handled == "dx" and shown(cb)[0].startswith(T("demo.not_running"))
        await click(ui, ALICE, ui.signer(ALICE)("ss", "escalation", "on"), state)
        assert (await ctx.storage.get_user(ALICE)).threshold.escalation_enabled

        sim = ctx.simulations.get_or_create(ALICE)
        taps: list[tuple] = []
        after_ack: list[str] = []
        claimed: list[str] = []

        async def on_event(event):
            data = event["data"]
            if claimed:
                if len(taps) == 2 and event["type"] in ("alert", "advice", "escalation"):
                    after_ack.append(event["type"])
                return
            if event["type"] != "advice" or not data.get("ack_id"):
                return
            claimed.append(data["ack_id"])
            alert = ui.alert_markup(ALICE, [("", f"ack:{data['ack_id']}"), ("", f"rescue:sim:{data['symbol']}")], lang=lang, demo=True)
            assert labels(alert) == [T("kb.alert_rescue"), T("kb.alert_awake"), T("kb.alert_snooze_demo"), T("kb.sim_stop")]
            rescue, awake = list(kb.iter_buttons(alert))[:2]
            for b in (rescue, awake):
                cb, handled = await click(ui, ALICE, b.callback_data, state)
                taps.append((handled, toast(cb)[0], shown(cb)[0]))

        sim.subscribe("click-through", on_event)
        await click(ui, ALICE, ui.signer(ALICE)("ds", "cross_longs"), state)
        await sim.wait()
        assert [t[0] for t in taps] == ["pr", "aa"], taps
        assert T("tools.name.rescue") in taps[0][2] and taps[1][1] == T("alerts.demo_acked_toast")
        assert after_ack == [] and sim.muted and sim.state == "finished"

        visited_sim, _ = await crawl(ui, ALICE, kb.demo_started(T, ui.signer(ALICE)), state)
        assert {"pl", "pd", "pr", "pw", "ps", "pf", "tr", "st", "ff"} <= visited_sim

        assert (await ui.services.binding.bind_hyperliquid(ALICE, WALLET)).ok
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("bu"), state)
        bx = next(b for b in kb.iter_buttons(shown(cb)[1]) if b.text.startswith("🗑"))
        cb, handled = await click(ui, ALICE, bx.callback_data, state)
        by = next(b for b in kb.iter_buttons(shown(cb)[1]) if b.text == T("kb.unbind_confirm"))
        cb, handled = await click(ui, ALICE, by.callback_data, state)
        assert handled == "by" and toast(cb)[0] == T("binding.unbound_toast")

        for action in ("dx", "do"):
            cb, handled = await click(ui, ALICE, ui.signer(ALICE)(action), state)
            assert handled == action and toast(cb)[0] != T(ERROR)
        assert not (await ctx.storage.get_user(ALICE)).is_demo_mode


# --------------------------------------------------------------------------- 9. guided binding


async def send_text(ui, uid, text, state, chat_type="private"):
    message = fake_message(uid, text, chat_type)
    message.bot = AsyncMock()
    calls = []
    message.delete = AsyncMock(side_effect=lambda: calls.append("delete"))
    message.bot.send_message = AsyncMock(side_effect=lambda *a, **k: calls.append("reply"))
    await ui.on_bind_message(message, state)
    reply = message.bot.send_message.await_args
    return message, calls, reply.args[1] if reply else None


class TestBindingFlow:
    async def test_binance_one_message(self, ctx, ui, fsm_storage, caplog, lang):
        T = Translator(lang)
        caplog.set_level(logging.DEBUG)
        state = state_for(fsm_storage, ALICE)
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("bb"), state)
        text, markup = shown(cb)
        assert text.startswith(T("binding.guide_binance").split("\n")[0]) and "Enable Reading" in text
        assert labels(markup) == [T("kb.bind_cancel")]
        assert await state.get_state() == BindStates.binance_key.state

        message, calls, reply = await send_text(ui, ALICE, f"{API_KEY}\n{API_SECRET}", state)
        assert calls[0] == "delete"
        assert T("binding.result.binance_ok", label="Binance", hint="…AB12", permissions="READ") in reply
        assert API_KEY not in reply and API_SECRET not in reply
        assert await state.get_state() is None and not (await state.get_data()).get("api_key")
        user = await ctx.storage.get_user(ALICE)
        assert user.bindings[0].api_key == API_KEY
        assert API_SECRET.encode() not in Path(ctx.settings.db_path).read_bytes()
        assert API_SECRET not in caplog.text and API_KEY not in caplog.text

    async def test_binance_key_then_secret(self, ctx, ui, fsm_storage):
        T = Translator(EN)
        state = state_for(fsm_storage, ALICE)
        await click(ui, ALICE, ui.signer(ALICE)("bb"), state)
        _, calls, reply = await send_text(ui, ALICE, API_KEY, state)
        assert calls == ["delete", "reply"] and T("binding.key_received") in reply and API_KEY not in reply
        assert await state.get_state() == BindStates.binance_secret.state
        _, calls, reply = await send_text(ui, ALICE, API_SECRET, state)
        assert calls[0] == "delete" and "…AB12" in reply
        assert not (await state.get_data()).get("api_key")

    async def test_trade_or_withdraw_permission_rejected(self, ctx, ui, fsm_storage, adapter):
        adapter.check = KeyCheck(False, "Rejected: key has FUTURES_TRADE permission", ["READ", "FUTURES_TRADE", "WITHDRAW"], True)
        state = state_for(fsm_storage, ALICE)
        await click(ui, ALICE, ui.signer(ALICE)("bb"), state)
        _, calls, reply = await send_text(ui, ALICE, f"{API_KEY} {API_SECRET}", state)
        assert calls[0] == "delete"
        assert Translator(EN)("binding.result.rejected_permissions", permissions="FUTURES_TRADE, WITHDRAW") in reply
        assert API_KEY not in reply
        assert (await ctx.storage.get_user(ALICE)) is None or not (await ctx.storage.get_user(ALICE)).bindings
        assert await state.get_state() == BindStates.binance_key.state

    @pytest.mark.parametrize("material", [
        " ".join(["abandon"] * 11 + ["about"]),
        "0x" + "a1" * 32,
    ])
    async def test_seed_phrase_or_private_key_never_stored(self, ctx, ui, fsm_storage, material):
        T = Translator(EN)
        for start, st, key in (("bb", BindStates.binance_key, "binding.secret_material"),
                               ("bh", BindStates.hyperliquid_address, "binding.result.secret_material_wallet")):
            state = state_for(fsm_storage, ALICE)
            await click(ui, ALICE, ui.signer(ALICE)(start), state)
            _, calls, reply = await send_text(ui, ALICE, material, state)
            assert calls[0] == "delete" and T(key) in reply and material not in reply
            assert await state.get_state() == st.state
        assert not FakeAdapter.seen
        assert await ui.services.binding.list(ALICE) == []

    async def test_hyperliquid_address(self, ctx, ui, fsm_storage, lang):
        T = Translator(lang)
        state = state_for(fsm_storage, ALICE)
        await click(ui, ALICE, ui.signer(ALICE)("bh"), state)
        _, calls, reply = await send_text(ui, ALICE, "0x1234", state)
        assert calls[0] == "delete" and T("binding.result.wallet_format") in reply
        _, _, reply = await send_text(ui, ALICE, WALLET, state)
        assert "Hyperliquid" in reply and "0xabab" in reply and WALLET not in reply
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("bl"), state)
        assert "Hyperliquid" in shown(cb)[0] and WALLET not in shown(cb)[0]
        assert (await ctx.storage.get_user(ALICE)).bindings[0].wallet_address == WALLET

    async def test_invalid_binance_format(self, ui, fsm_storage):
        state = state_for(fsm_storage, ALICE)
        await click(ui, ALICE, ui.signer(ALICE)("bb"), state)
        _, calls, reply = await send_text(ui, ALICE, "short nope", state)
        assert calls[0] == "delete" and Translator(EN)("binding.result.key_format") in reply
        assert not FakeAdapter.seen

    @pytest.mark.parametrize("word", ["cancel", "取消"])
    async def test_cancel(self, ui, fsm_storage, word):
        T = Translator(EN)
        state = state_for(fsm_storage, ALICE)
        await click(ui, ALICE, ui.signer(ALICE)("bb"), state)
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("bc"), state)
        assert await state.get_state() is None and shown(cb)[0].startswith(T("binding.menu", bound="").split("\n")[0])
        await click(ui, ALICE, ui.signer(ALICE)("bh"), state)
        _, _, reply = await send_text(ui, ALICE, word, state)
        assert reply == T("binding.cancelled") and await state.get_state() is None

    async def test_group_chat_refused(self, ui, fsm_storage):
        state = state_for(fsm_storage, ALICE)
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("bb"), state, chat_type="group")
        assert shown(cb)[0] == Translator(EN)("binding.private_only") and toast(cb)[1] is True
        assert await state.get_state() is None

    async def test_other_commands_pass_through(self, ui, fsm_storage):
        state = state_for(fsm_storage, ALICE)
        await state.set_state(BindStates.binance_key)
        with pytest.raises(SkipHandler):
            await ui.on_bind_message(fake_message(ALICE, "/risk"), state)

    async def test_unbind_needs_confirmation(self, ui, lang):
        T = Translator(lang)
        assert (await ui.services.binding.bind_hyperliquid(ALICE, WALLET)).ok
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("bx", "hyperliquid", "mainnet"))
        text, markup = shown(cb)
        assert text.startswith("⚠️") and labels(markup) == [T("kb.unbind_confirm"), T("kb.cancel")]
        assert len(await ui.services.binding.list(ALICE)) == 1
        cancel = next(b for b in kb.iter_buttons(markup) if b.text == T("kb.cancel"))
        cb, handled = await click(ui, ALICE, cancel.callback_data)
        assert handled == "bu" and len(await ui.services.binding.list(ALICE)) == 1

    async def test_menu_shows_security_note_and_masked_bindings(self, ui, lang):
        assert (await ui.services.binding.bind_binance(ALICE, API_KEY, API_SECRET)).ok
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("b"))
        text = shown(cb)[0]
        assert security_note(lang) in text and "…AB12" in text and API_KEY not in text


# --------------------------------------------------------------------------- 10. alert buttons on live and simulated alerts


class TestAlertButtons:
    @pytest.mark.parametrize("alert_lang", LANGUAGES)
    async def test_notifier_buttons_use_the_alerts_language(self, ui, alert_lang):
        T = Translator(alert_lang)
        bot = AsyncMock()
        notifier = TelegramNotifier(bot, ui.alert_markup)
        await notifier.notify(ALICE, "🚨", [("🛟", "rescue:binance:BTCUSDT"), ("✅", "ack:AbCdEf123_-x")], lang=alert_lang)
        markup = bot.send_message.await_args.kwargs["reply_markup"]
        assert labels(markup) == [T("kb.alert_rescue"), T("kb.alert_awake"), T("kb.alert_snooze")]
        assert [a for a, _ in actions_of(ui, ALICE, markup)] == ["pr", "aa", "az"]
        for b in kb.iter_buttons(markup):
            with pytest.raises(CallbackError):
                ui.codec.decode(BOB, b.callback_data)

    async def test_notifier_without_ui_keeps_legacy_buttons(self):
        bot = AsyncMock()
        await TelegramNotifier(bot).notify(ALICE, "x", [("🛟 Rescue options", "rescue:binance:BTCUSDT")])
        assert [b.callback_data for b in kb.iter_buttons(bot.send_message.await_args.kwargs["reply_markup"])] == ["rescue:binance:BTCUSDT"]

    async def test_unsignable_symbol_falls_back_to_legacy(self, ui):
        bot = AsyncMock()
        await TelegramNotifier(bot, ui.alert_markup).notify(ALICE, "x", [("🛟", "rescue:hyperliquid:@107")])
        assert [b.callback_data for b in kb.iter_buttons(bot.send_message.await_args.kwargs["reply_markup"])] == ["rescue:hyperliquid:@107"]

    async def test_live_alert_body_and_buttons_share_the_users_language(self, ctx, ui, lang):
        from liqguard.alerts import AlertManager, EscalationService, PositionMonitor
        from liqguard.models import UserThreshold

        await enable_demo(ctx, ALICE, "critical")
        user = await ctx.storage.get_user(ALICE)
        user.threshold = UserThreshold(adaptive_thresholds=False)
        bot = AsyncMock()
        notifier = TelegramNotifier(bot, ui.alert_markup)

        async def users():
            return [user]

        monitor = PositionMonitor(users, ctx.positions.snapshot_for, notifier, AlertManager(), ctx.llm,
                                  escalation=EscalationService(notifier))
        await monitor.check_all_users()
        sent = [(c.args[1], c.kwargs.get("reply_markup")) for c in bot.send_message.await_args_list]
        assert any(markup and Translator(lang)("kb.alert_awake") in labels(markup) for _, markup in sent)
        for text, markup in sent:
            assert in_language(lang, text, markup), (lang, text[:120], labels(markup))

    async def test_live_snooze_holds_reminders(self, ctx, ui):
        now = time.time()
        ack = ctx.escalations.open(ALICE, "binance", "BTCUSDT", "critical", "x", now - 120, 60, 5)
        assert ack in ctx.escalations.due(now)
        await click(ui, ALICE, ui.signer(ALICE)("az", ack.id))
        assert ack not in ctx.escalations.due(now + 60)
        assert ack in ctx.escalations.due(now + 31 * 60)


# --------------------------------------------------------------------------- 11. demo: one language, ⏹ everywhere, silent after ✅ / 😴 / ❌ / ⏹


def sent_messages(bot) -> list[tuple[str, object]]:
    return [(c.args[1], c.kwargs.get("reply_markup")) for c in bot.send_message.await_args_list]


def button_for(ui, markup, action: str):
    return next((b for b in kb.iter_buttons(markup) if b.callback_data and is_ui_payload(b.callback_data)
                 and ui.codec.decode(ALICE, b.callback_data).action == action), None)


class TestDemoIsBoundedAndSilencedByTheUser:
    SETTLE = 0.3

    async def start(self, ctx, ui, scenario: str = "mixed"):
        bot = AsyncMock()
        sim = ctx.simulations.get_or_create(ALICE)
        sim.tick_interval, sim.escalation_seconds_per_minute = 0.03, 0.002
        TelegramSimulationBridge(bot, sim, ui.alert_markup)
        cb, handled = await click(ui, ALICE, ui.signer(ALICE)("ds", scenario))
        assert handled == "ds" and sim.is_running
        return bot, sim

    async def first_message_with(self, ui, bot, action: str):
        assert await until(lambda: any(m and button_for(ui, m, action) for _, m in sent_messages(bot))), action
        return next((t, m) for t, m in sent_messages(bot) if m and button_for(ui, m, action))

    async def assert_silent_from_now(self, bot, sim):
        sent = bot.send_message.await_count
        await sim.wait()
        await asyncio.sleep(self.SETTLE)
        assert bot.send_message.await_count == sent, [t[:60] for t, _ in sent_messages(bot)[sent:]]
        assert sim.escalation.pending_for(ALICE) == []
        assert sim._escalation_task is None or sim._escalation_task.done()

    async def test_start_demo_press_im_awake_no_further_demo_messages(self, ctx, ui, lang):
        """The live bug: ✅ 我醒了 on a demo alert, yet the demo kept raising new alerts and reminders."""
        T = Translator(lang)
        bot, sim = await self.start(ctx, ui)
        _, markup = await self.first_message_with(ui, bot, "aa")
        cb, handled = await click(ui, ALICE, button_for(ui, markup, "aa").callback_data)
        assert handled == "aa" and toast(cb)[0] == T("alerts.demo_acked_toast")
        assert sim.muted and sim.is_running
        await self.assert_silent_from_now(bot, sim)
        assert sim.state == "finished" and sim.step == sim.steps

    async def test_awake_after_reminders_started(self, ctx, ui):
        bot, sim = await self.start(ctx, ui)
        assert await until(lambda: len(sim.escalation_log) >= 2)
        _, markup = await self.first_message_with(ui, bot, "aa")
        await click(ui, ALICE, button_for(ui, markup, "aa").callback_data)
        await self.assert_silent_from_now(bot, sim)

    async def test_mute_this_demo_stops_everything(self, ctx, ui):
        bot, sim = await self.start(ctx, ui)
        _, markup = await self.first_message_with(ui, bot, "az")
        cb, handled = await click(ui, ALICE, button_for(ui, markup, "az").callback_data)
        assert handled == "az" and toast(cb)[0] == Translator(EN)("alerts.demo_quiet_toast")
        await self.assert_silent_from_now(bot, sim)

    async def test_dismiss_stops_everything(self, ctx, ui):
        from .conftest import fake_callback

        bot, sim = await self.start(ctx, ui)
        assert await until(lambda: any(m and any(b.callback_data == "action:dismiss" for b in kb.iter_buttons(m))
                                       for _, m in sent_messages(bot)))
        await BotHandlers(ctx).callback_action_confirm(fake_callback(ALICE, "action:dismiss"))
        await self.assert_silent_from_now(bot, sim)

    async def test_stop_button_on_a_demo_alert_stops_everything(self, ctx, ui, lang):
        bot, sim = await self.start(ctx, ui)
        _, markup = await self.first_message_with(ui, bot, "aa")
        cb, handled = await click(ui, ALICE, button_for(ui, markup, "dx").callback_data)
        assert handled == "dx" and shown(cb)[0].startswith(Translator(lang)("demo.stopped"))
        assert sim.state == "stopped"
        await self.assert_silent_from_now(bot, sim)

    async def test_stopdemo_command(self, ctx, ui, fsm_storage, lang):
        bot, sim = await self.start(ctx, ui)
        await self.first_message_with(ui, bot, "aa")
        message = fake_message(ALICE, "/stopdemo")
        message.bot = AsyncMock()
        await ui.on_command(message, state_for(fsm_storage, ALICE), MagicMock(command="stopdemo", args=None))
        assert message.answer.await_args.args[0].startswith(Translator(lang)("demo.stopped"))
        assert sim.state == "stopped"
        await self.assert_silent_from_now(bot, sim)

    async def test_untouched_demo_is_finite_and_every_message_can_stop_it(self, ctx, ui, lang):
        T = Translator(lang)
        bot, sim = await self.start(ctx, ui)
        await sim.wait()
        await asyncio.sleep(self.SETTLE)
        sent = sent_messages(bot)
        assert sim.state == "finished" and sim.step == sim.steps
        assert sim.escalation.pending_for(ALICE) == [] and (sim._escalation_task is None or sim._escalation_task.done())
        *during, (last, last_markup) = sent
        assert last.startswith(T("sim.finished").split("\n")[0]) and last_markup is None
        assert during and all(m is not None and button_for(ui, m, "dx") for _, m in during)
        count = bot.send_message.await_count
        await asyncio.sleep(self.SETTLE)
        assert bot.send_message.await_count == count

    async def test_every_demo_message_and_its_keyboard_share_the_owners_language(self, ctx, ui, lang):
        bot, sim = await self.start(ctx, ui, "cross_longs")
        await sim.wait()
        sent = sent_messages(bot)
        kinds = {t.split("\n")[0] for t, _ in sent}
        assert len(sent) > 5 and len(kinds) > 3
        for text, markup in sent:
            assert in_language(lang, text, markup), (lang, text[:120], labels(markup))
        await sim.stop()

    async def test_language_switch_mid_demo_changes_body_and_buttons_together(self, ctx, ui):
        bot, sim = await self.start(ctx, ui)
        await self.first_message_with(ui, bot, "aa")
        await click(ui, ALICE, ui.signer(ALICE)("ls", "zh"))
        before = bot.send_message.await_count
        await sim.wait()
        for text, markup in sent_messages(bot)[before:]:
            assert in_language(ZH, text, markup), (text[:120], labels(markup))
