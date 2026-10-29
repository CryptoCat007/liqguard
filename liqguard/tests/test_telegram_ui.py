"""
Inline-keyboard UI (zh-Hant-TW): callback codec, keyboard builders, router
dispatch, user scoping, answerCallbackQuery, stale buttons, an end-to-end
demo click-through and the guided binding flow.
"""

import ast
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from liqguard.core.binding import BindingService
from liqguard.exchanges.base import KeyCheck
from liqguard.telegram import BotHandlers, TelegramNotifier
from liqguard.telegram.sim_bridge import TelegramSimulationBridge
from liqguard.telegram.ui import BOT_COMMANDS, TelegramUI
from liqguard.telegram.ui import keyboards as kb
from liqguard.telegram.ui.actions import ACTIONS
from liqguard.telegram.ui.codec import MAX_BYTES, CallbackCodec, CallbackError, is_ui_payload
from liqguard.telegram.ui.features import FEATURES, dispatch_table
from liqguard.telegram.ui.features.binding import BindStates
from liqguard.telegram.ui.router import ERROR, EXPIRED, INVALID, CallbackRouter
from liqguard.telegram.ui.screen import Signer
from liqguard.telegram.ui.services import UIServices
from liqguard.telegram.ui.texts import NOT_FOUND

from .conftest import ALICE, BOB, fake_message

UI_DIR = Path(__file__).resolve().parents[1] / "telegram" / "ui"
API_KEY = "Kq7" + "x" * 50 + "AB12"
API_SECRET = "Sz9" + "y" * 50 + "CD34"
WALLET = "0x" + "ab" * 20

SAMPLE_ARGS = {
    "m": (), "h": (), "pl": (), "tr": (), "st": (), "ff": (), "se": (), "b": (), "bb": (), "bh": (), "bl": (), "bu": (),
    "bc": (), "db": (), "dm": (), "dx": (), "do": (),
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


def state_for(storage, uid: int) -> FSMContext:
    return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def make_callback(uid: int, data, chat_type: str = "private"):
    cb = AsyncMock()
    cb.from_user = MagicMock(id=uid)
    cb.data = data
    cb.message = AsyncMock()
    cb.message.chat = MagicMock(type=chat_type, id=uid)
    cb.bot = AsyncMock()
    return cb


async def click(ui, uid, data, state=None, chat_type="private"):
    cb = make_callback(uid, data, chat_type)
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


def all_keyboards(sign: Signer) -> list:
    """Every keyboard builder with representative data (the source of every button the bot can render)."""
    settings = {"warning": 20, "critical": 10, "emergency": 5, "interval": 2, "max": 5, "quiet_label": "關閉",
                "escalation": True, "adaptive": False, "stops": True}
    return [
        kb.main_menu(sign),
        kb.positions_list(sign, [("sim", "BTCUSDT", "🆘 BTCUSDT 多 · 3.0%"), ("binance", "ETHUSDT", "⚠️ ETHUSDT 空 · 12.0%")]),
        kb.position_tools(sign, "sim", "BTCUSDT"),
        kb.position_tools(sign, "sim", "BTCUSDT", current="pr"),
        kb.triage(sign, [("sim", "BTCUSDT", "救 BTCUSDT（第 1 名）")]),
        kb.stress(sign, [("sim", "BTCUSDT")]),
        kb.refresh_back(sign, "ff"),
        kb.no_account(sign),
        kb.back_only(sign),
        kb.settings(sign, settings),
        *[kb.setting_options(sign, key, values, values[0]) for key, values in
          {"warning": (15, 20), "critical": (8, 10), "emergency": (3, 5), "interval": (1, 2), "max": (3, 5),
           "quiet": ("off", "23-7")}.items()],
        kb.binding_menu(sign),
        kb.binding_waiting(sign),
        kb.binding_back(sign),
        kb.unbind_list(sign, [("binance", "mainnet", "🟡 Binance …AB12"), ("hyperliquid", "mainnet", "🟢 Hyperliquid 0xabab…abab")]),
        kb.unbind_confirm(sign, "binance", "mainnet"),
        kb.dashboard(sign, "https://liqguard.test/auth/login?token=x"),
        kb.demo_menu(sign, running=False, demo_mode=True),
        kb.demo_menu(sign, running=True, demo_mode=True),
        kb.demo_started(sign),
        kb.alert_keyboard(sign, "sim", "BTCUSDT", "AbCdEf123_-x"),
    ]


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

    def test_main_menu_has_the_nine_entries(self):
        assert labels(kb.main_menu(self.sign)) == [
            "📊 我的倉位", "🚨 先處理哪個", "🧪 壓力測試", "💸 資金費率/手續費", "🔔 通知設定",
            "🔑 綁定帳號", "🖥️ 網頁看板", "🎬 Demo", "❓ 幫助",
        ]

    def test_every_position_gets_its_four_tools_and_a_way_back(self):
        markup = kb.positions_list(self.sign, [("sim", "BTCUSDT", "BTC"), ("sim", "ETHUSDT", "ETH")])
        names = labels(markup)
        assert names == ["BTC", "🛟 救倉試算", "🕯 插針壓測", "🛑 止損檢查", "💸 資金費影響",
                         "ETH", "🛟 救倉試算", "🕯 插針壓測", "🛑 止損檢查", "💸 資金費影響",
                         "🔄 重新整理", "⬅️ 返回主選單"]
        acts = [a for a, _ in actions_of_codec(self.sign, markup)]
        assert acts[:5] == ["pd", "pr", "pw", "ps", "pf"]

    def test_tool_screen_hides_the_current_tool(self):
        names = labels(kb.position_tools(self.sign, "sim", "BTCUSDT", current="pw"))
        assert "🕯 插針壓測" not in names and "🛟 救倉試算" in names and names[-1] == "⬅️ 返回主選單"

    def test_alert_buttons(self):
        assert labels(kb.alert_keyboard(self.sign, "sim", "BTCUSDT", "abcdef123")) == [
            "🚑 先處理這個", "✅ 我醒了", "😴 暫停提醒 30 分鐘"]
        assert labels(kb.alert_keyboard(self.sign, "sim", "BTCUSDT", None)) == ["🚑 先處理這個"]
        assert kb.alert_keyboard(self.sign, None, None, None) is None

    def test_builders_are_pure(self):
        assert [m.model_dump() for m in all_keyboards(self.sign)] == [m.model_dump() for m in all_keyboards(self.sign)]

    def test_every_button_fits_and_is_signed_for_its_user(self):
        codec = self.sign.codec
        for markup in all_keyboards(self.sign):
            for b in kb.iter_buttons(markup):
                if b.url:
                    continue
                assert len(b.callback_data.encode()) <= MAX_BYTES
                codec.decode(ALICE, b.callback_data, now=1_800_000_000)
                with pytest.raises(CallbackError):
                    codec.decode(BOB, b.callback_data, now=1_800_000_000)

    def test_dashboard_url_button_only_for_https(self):
        assert labels(kb.dashboard(self.sign, "http://localhost/x")) == ["⬅️ 返回主選單"]
        assert labels(kb.dashboard(self.sign, "https://x.test/y"))[0] == "🖥️ 開啟我的看板"

    def test_all_ui_text_is_traditional_chinese(self):
        simplified = set("险仓设绑帐户启关闭页种发误确认损费详")
        for markup in all_keyboards(self.sign):
            for name in labels(markup):
                assert not simplified & set(name), name


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
                assert module.split(".")[0] in ("__future__", "typing", "aiogram"), module

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
        reachable = {a for m in all_keyboards(sign) for a, _ in actions_of(ui, ALICE, m)}
        assert reachable == set(ACTIONS)

    async def test_every_button_routes_to_its_handler(self, ctx, ui, fsm_storage):
        await enable_demo(ctx, ALICE)
        sign = ui.signer(ALICE)
        checked = 0
        for markup in all_keyboards(sign):
            for b in kb.iter_buttons(markup):
                if not b.callback_data:
                    continue
                expected = ui.codec.decode(ALICE, b.callback_data).action
                cb, handled = await click(ui, ALICE, b.callback_data, state_for(fsm_storage, ALICE))
                assert handled == expected, (b.text, handled, expected)
                assert toast(cb)[0] != ERROR, b.text
                assert cb.answer.await_count == 1
                checked += 1
        assert checked > 60

    async def test_legacy_callbacks_are_left_to_bot_handlers(self, ctx, ui):
        dp = Dispatcher(storage=MemoryStorage())
        ui.register(dp)
        BotHandlers(ctx).register(dp, AsyncMock())
        assert dp.sub_routers[0] is ui.aiogram_router
        assert not is_ui_payload("rescue:sim:BTCUSDT")

    async def test_set_my_commands(self, ui):
        bot = AsyncMock()
        await ui.set_commands(bot)
        commands = bot.set_my_commands.await_args.args[0]
        assert [c.command for c in commands] == [c for c, _ in BOT_COMMANDS]
        assert {"start", "menu"} <= {c.command for c in commands}
        assert all(c.command.islower() and len(c.command) <= 32 and c.description for c in commands)
        bot.set_my_commands.side_effect = RuntimeError("telegram down")
        await ui.set_commands(bot)

    @pytest.mark.parametrize("text,command,action", [
        ("/start", "start", "m"), ("/menu", "menu", "m"), ("/help", "help", "h"),
        ("/positions", "positions", "pl"), ("/bind", "bind", "b"), ("/demo", "demo", "dm"), ("/settings", "settings", "se"),
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

    async def test_start_shows_main_menu(self, ui, fsm_storage):
        message = fake_message(ALICE, "/start")
        message.bot = AsyncMock()
        await ui.on_command(message, state_for(fsm_storage, ALICE), MagicMock(command="start", args=None))
        text, markup = message.answer.await_args.args[0], message.answer.await_args.kwargs["reply_markup"]
        assert text.startswith("🏠 主選單")
        assert len(labels(markup)) == 9

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
        assert toast(cb) == (INVALID, True)
        cb.message.edit_text.assert_not_awaited()
        cb.message.answer.assert_not_awaited()

    async def test_buttons_differ_per_user(self, ui):
        assert ui.signer(ALICE)("pr", "sim", "BTCUSDT") != ui.signer(BOB)("pr", "sim", "BTCUSDT")

    async def test_b_signing_a_position_reference_only_sees_its_own_book(self, ctx, ui):
        await enable_demo(ctx, ALICE)
        for action in ("pd", "pr", "pw", "ps", "pf"):
            cb, handled = await click(ui, BOB, ui.signer(BOB)(action, "demo", "BTCUSDT"))
            text, _ = shown(cb)
            assert handled == action and text == NOT_FOUND
        cb, _ = await click(ui, BOB, ui.signer(BOB)("pl"))
        assert "BTCUSDT" not in shown(cb)[0]

    async def test_b_cannot_ack_or_snooze_a_alert(self, ctx, ui):
        ack = ctx.escalations.open(ALICE, "binance", "BTCUSDT", "critical", "x", time.time(), 60, 5)
        for action in ("aa", "az"):
            cb, _ = await click(ui, BOB, ui.signer(BOB)(action, ack.id))
            assert "已確認或已失效" in toast(cb)[0]
        assert ack.snoozed_until is None and ack.acknowledged_at is None
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("az", ack.id))
        assert "30" in toast(cb)[0] and ack.snoozed_until >= time.time() + 29 * 60
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("aa", ack.id))
        assert ack.acknowledged_at is not None

    async def test_b_cannot_unbind_a(self, ctx, ui):
        assert (await ui.services.binding.bind_hyperliquid(ALICE, WALLET)).ok
        cb, _ = await click(ui, BOB, ui.signer(BOB)("by", "hyperliquid", "mainnet"))
        assert "不存在" in shown(cb)[0]
        assert len(await ui.services.binding.list(ALICE)) == 1

    async def test_settings_change_only_the_callers_thresholds(self, ctx, ui):
        await click(ui, ALICE, ui.signer(ALICE)("ss", "warning", "30"))
        assert (await ctx.storage.get_user(ALICE)).threshold.warning_distance_pct == 30
        bob = await ui.services.user(BOB)
        assert bob.threshold.warning_distance_pct == ctx.settings.default_threshold().warning_distance_pct

    async def test_demo_simulation_is_private(self, ctx, ui):
        await click(ui, ALICE, ui.signer(ALICE)("ds", "cross_longs"))
        assert ctx.simulations.get(ALICE) is not None and ctx.simulations.get(BOB) is None
        cb, _ = await click(ui, BOB, ui.signer(BOB)("pl"))
        assert shown(cb)[0].startswith("🔌")


# --------------------------------------------------------------------------- 6. answerCallbackQuery always


class TestAlwaysAnswered:
    async def test_success_invalid_and_none(self, ui):
        for data in (ui.signer(ALICE)("m"), "1.m.0.AAAAAAAA", None, "", "x" * 200):
            cb, _ = await click(ui, ALICE, data)
            assert cb.answer.await_count == 1

    async def test_handler_exception(self, ui, monkeypatch):
        async def boom(req):
            raise RuntimeError("db down")

        monkeypatch.setitem(ui.router.handlers, "pl", boom)
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("pl"))
        assert cb.answer.await_count == 1
        assert toast(cb) == (ERROR, True)

    async def test_render_failure(self, ui):
        cb = make_callback(ALICE, ui.signer(ALICE)("m"))
        cb.message.edit_text.side_effect = RuntimeError("message can't be edited")
        cb.message.answer.side_effect = RuntimeError("chat not found")
        cb.bot.send_message.side_effect = RuntimeError("blocked by user")
        await ui.router.handle(cb)
        assert cb.answer.await_count == 1
        assert toast(cb) == (ERROR, True)

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
            assert cb.answer.await_count == 1 and toast(cb)[0] == ERROR


# --------------------------------------------------------------------------- 7. stale / expired buttons


class TestStaleButtons:
    async def test_expired_button_opens_a_fresh_main_menu(self, ctx, ui):
        await enable_demo(ctx, ALICE)
        old = ui.codec.encode(ALICE, "pr", "demo", "BTCUSDT", now=time.time() - 3 * 86400)
        cb, handled = await click(ui, ALICE, old)
        text, markup = shown(cb)
        assert handled == "m" and text.startswith(EXPIRED)
        assert toast(cb)[0] == "按鈕已過期"
        assert labels(markup)[0] == "📊 我的倉位"

    async def test_expired_unbind_confirmation_does_nothing(self, ui):
        assert (await ui.services.binding.bind_hyperliquid(ALICE, WALLET)).ok
        old = ui.codec.encode(ALICE, "by", "hyperliquid", "mainnet", now=time.time() - 180)
        cb, handled = await click(ui, ALICE, old)
        assert handled == "m"
        assert len(await ui.services.binding.list(ALICE)) == 1

    async def test_closed_position(self, ctx, ui):
        await enable_demo(ctx, ALICE)
        cb, handled = await click(ui, ALICE, ui.signer(ALICE)("pw", "demo", "NOPEUSDT"))
        assert handled == "pw" and shown(cb)[0] == NOT_FOUND and toast(cb)[0] == "倉位已不存在"

    async def test_simulation_buttons_after_demo_is_disabled(self, ctx, ui):
        await click(ui, ALICE, ui.signer(ALICE)("ds", "cross_longs"))
        stale = ui.signer(ALICE)("pr", "sim", "BTCUSDT")
        await click(ui, ALICE, ui.signer(ALICE)("do"))
        cb, _ = await click(ui, ALICE, stale)
        assert shown(cb)[0] == NOT_FOUND

    async def test_already_acknowledged_alert(self, ctx, ui):
        ack = ctx.escalations.open(ALICE, "binance", "BTCUSDT", "critical", "x", time.time(), 60, 5)
        await click(ui, ALICE, ui.signer(ALICE)("aa", ack.id))
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("aa", ack.id))
        assert "已確認或已失效" in toast(cb)[0]
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
        assert handled is None and toast(cb) == (INVALID, True)


# --------------------------------------------------------------------------- 8. end-to-end demo click-through


async def crawl(ui, uid, start_markup, state, skip=frozenset({"do", "dx", "by", "bb", "bh", "db"}), limit=400):
    """Breadth-first tap of every distinct button reachable from `start_markup`."""
    visited: set[str] = set()
    seen: set[tuple] = set()
    queue = []
    for b in kb.iter_buttons(start_markup):
        queue.append(b.callback_data)
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
        assert toast(cb)[0] != ERROR, (d.action, d.args)
        assert cb.answer.await_count == 1
        text, markup = shown(cb)
        assert text, (d.action, d.args)
        visited.add(handled)
        for b in kb.iter_buttons(markup):
            if b.callback_data and ui.codec.decode(uid, b.callback_data).action not in skip:
                queue.append(b.callback_data)
    return visited, seen


class TestDemoClickThrough:
    async def test_every_menu_path_in_demo_mode(self, ctx, ui, fsm_storage):
        state = state_for(fsm_storage, ALICE)
        start = fake_message(ALICE, "/start")
        start.bot = AsyncMock()
        await ui.on_command(start, state, MagicMock(command="start", args=None))
        menu = start.answer.await_args.kwargs["reply_markup"]

        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("ds", "critical"), state)
        visited, seen = await crawl(ui, ALICE, menu, state)
        expected = set(ACTIONS) - {"do", "dx", "by", "bb", "bh", "bc", "db", "bx", "aa", "az"}
        assert expected <= visited, expected - visited
        tools = {(a, args) for a, args in seen if a in ("pr", "pw", "ps", "pf")}
        assert {args for _, args in tools} >= {("sim", s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "AVAXUSDT")} \
            or {args for _, args in tools} >= {("demo", s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")}

        for action in ("db", "bb", "bh", "bc"):
            cb, handled = await click(ui, ALICE, ui.signer(ALICE)(action), state)
            assert handled == action and toast(cb)[0] != ERROR

        cb, handled = await click(ui, ALICE, ui.signer(ALICE)("dx"), state)
        assert handled == "dx" and shown(cb)[0].startswith("⏹ 模擬已停止")
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("ss", "escalation", "on"), state)
        assert "喚醒提醒：開" in shown(cb)[0]
        sim = ctx.simulations.get_or_create(ALICE)
        taps: list[tuple] = []

        async def on_event(event):
            data = event["data"]
            if taps or event["type"] not in ("advice", "escalation") or not data.get("ack_id"):
                return
            alert = ui.alert_markup(ALICE, [("", f"ack:{data['ack_id']}"), ("", f"rescue:sim:{data['symbol']}")])
            rescue, awake, snooze = kb.iter_buttons(alert)
            assert [rescue.text, awake.text, snooze.text] == ["🚑 先處理這個", "✅ 我醒了", "😴 暫停提醒 30 分鐘"]
            for b in (snooze, rescue, awake):
                cb, handled = await click(ui, ALICE, b.callback_data, state)
                taps.append((handled, toast(cb)[0], shown(cb)[0]))

        sim.subscribe("click-through", on_event)
        await click(ui, ALICE, ui.signer(ALICE)("ds", "cross_longs"), state)
        await sim.wait()
        assert [t[0] for t in taps] == ["az", "pr", "aa"], taps
        assert "30 分鐘" in taps[0][1] and "救倉試算" in taps[1][2] and taps[2][1] == "已停止提醒"

        visited_sim, _ = await crawl(ui, ALICE, kb.demo_started(ui.signer(ALICE)), state)
        assert {"pl", "pd", "pr", "pw", "ps", "pf", "tr", "st", "ff"} <= visited_sim

        assert (await ui.services.binding.bind_hyperliquid(ALICE, WALLET)).ok
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("bu"), state)
        bx = next(b for b in kb.iter_buttons(shown(cb)[1]) if b.text.startswith("🗑"))
        cb, handled = await click(ui, ALICE, bx.callback_data, state)
        by = next(b for b in kb.iter_buttons(shown(cb)[1]) if b.text == "✅ 確認解除")
        cb, handled = await click(ui, ALICE, by.callback_data, state)
        assert handled == "by" and "已解除" in shown(cb)[0]

        for action in ("dx", "do"):
            cb, handled = await click(ui, ALICE, ui.signer(ALICE)(action), state)
            assert handled == action and toast(cb)[0] != ERROR
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
    async def test_binance_one_message(self, ctx, ui, fsm_storage, caplog):
        caplog.set_level(logging.DEBUG)
        state = state_for(fsm_storage, ALICE)
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("bb"), state)
        text, markup = shown(cb)
        assert "Enable Reading" in text and "唯讀" in text and "私鑰" in text
        assert labels(markup) == ["❌ 取消綁定"]
        assert await state.get_state() == BindStates.binance_key.state

        message, calls, reply = await send_text(ui, ALICE, f"{API_KEY}\n{API_SECRET}", state)
        assert calls[0] == "delete"
        assert "已綁定 Binance" in reply and "…AB12" in reply
        assert API_KEY not in reply and API_SECRET not in reply
        assert await state.get_state() is None and not (await state.get_data()).get("api_key")
        user = await ctx.storage.get_user(ALICE)
        assert user.bindings[0].api_key == API_KEY
        assert API_SECRET.encode() not in Path(ctx.settings.db_path).read_bytes()
        assert API_SECRET not in caplog.text and API_KEY not in caplog.text

    async def test_binance_key_then_secret(self, ctx, ui, fsm_storage):
        state = state_for(fsm_storage, ALICE)
        await click(ui, ALICE, ui.signer(ALICE)("bb"), state)
        _, calls, reply = await send_text(ui, ALICE, API_KEY, state)
        assert calls == ["delete", "reply"] and "Secret" in reply and API_KEY not in reply
        assert await state.get_state() == BindStates.binance_secret.state
        _, calls, reply = await send_text(ui, ALICE, API_SECRET, state)
        assert calls[0] == "delete" and "已綁定" in reply
        assert not (await state.get_data()).get("api_key")

    async def test_trade_or_withdraw_permission_rejected(self, ctx, ui, fsm_storage, adapter):
        adapter.check = KeyCheck(False, "Rejected: key has FUTURES_TRADE permission", ["READ", "FUTURES_TRADE", "WITHDRAW"], True)
        state = state_for(fsm_storage, ALICE)
        await click(ui, ALICE, ui.signer(ALICE)("bb"), state)
        _, calls, reply = await send_text(ui, ALICE, f"{API_KEY} {API_SECRET}", state)
        assert calls[0] == "delete"
        assert "已拒絕" in reply and "FUTURES_TRADE" in reply and "WITHDRAW" in reply
        assert API_KEY not in reply
        assert (await ctx.storage.get_user(ALICE)) is None or not (await ctx.storage.get_user(ALICE)).bindings
        assert await state.get_state() == BindStates.binance_key.state

    @pytest.mark.parametrize("material", [
        " ".join(["abandon"] * 11 + ["about"]),
        "0x" + "a1" * 32,
    ])
    async def test_seed_phrase_or_private_key_never_stored(self, ctx, ui, fsm_storage, material):
        for start, st in (("bb", BindStates.binance_key), ("bh", BindStates.hyperliquid_address)):
            state = state_for(fsm_storage, ALICE)
            await click(ui, ALICE, ui.signer(ALICE)(start), state)
            _, calls, reply = await send_text(ui, ALICE, material, state)
            assert calls[0] == "delete" and "私鑰或助記詞" in reply and material not in reply
            assert await state.get_state() == st.state
        assert not FakeAdapter.seen
        assert await ui.services.binding.list(ALICE) == []

    async def test_hyperliquid_address(self, ctx, ui, fsm_storage):
        state = state_for(fsm_storage, ALICE)
        await click(ui, ALICE, ui.signer(ALICE)("bh"), state)
        _, calls, reply = await send_text(ui, ALICE, "0x1234", state)
        assert calls[0] == "delete" and "格式不正確" in reply
        _, _, reply = await send_text(ui, ALICE, WALLET, state)
        assert "已綁定 Hyperliquid" in reply and WALLET not in reply
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("bl"), state)
        assert "Hyperliquid" in shown(cb)[0] and WALLET not in shown(cb)[0]
        assert (await ctx.storage.get_user(ALICE)).bindings[0].wallet_address == WALLET

    async def test_invalid_binance_format(self, ui, fsm_storage):
        state = state_for(fsm_storage, ALICE)
        await click(ui, ALICE, ui.signer(ALICE)("bb"), state)
        _, calls, reply = await send_text(ui, ALICE, "short nope", state)
        assert calls[0] == "delete" and "格式不正確" in reply
        assert not FakeAdapter.seen

    async def test_cancel(self, ui, fsm_storage):
        state = state_for(fsm_storage, ALICE)
        await click(ui, ALICE, ui.signer(ALICE)("bb"), state)
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("bc"), state)
        assert await state.get_state() is None and shown(cb)[0].startswith("🔑 綁定帳號")
        await click(ui, ALICE, ui.signer(ALICE)("bh"), state)
        _, _, reply = await send_text(ui, ALICE, "取消", state)
        assert reply == "已取消綁定。" and await state.get_state() is None

    async def test_group_chat_refused(self, ui, fsm_storage):
        state = state_for(fsm_storage, ALICE)
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("bb"), state, chat_type="group")
        assert "私訊" in shown(cb)[0] and toast(cb)[1] is True
        assert await state.get_state() is None

    async def test_other_commands_pass_through(self, ui, fsm_storage):
        state = state_for(fsm_storage, ALICE)
        await state.set_state(BindStates.binance_key)
        with pytest.raises(SkipHandler):
            await ui.on_bind_message(fake_message(ALICE, "/risk"), state)

    async def test_unbind_needs_confirmation(self, ui):
        assert (await ui.services.binding.bind_hyperliquid(ALICE, WALLET)).ok
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("bx", "hyperliquid", "mainnet"))
        text, markup = shown(cb)
        assert "確定要解除" in text and labels(markup) == ["✅ 確認解除", "❌ 取消"]
        assert len(await ui.services.binding.list(ALICE)) == 1
        cancel = next(b for b in kb.iter_buttons(markup) if b.text == "❌ 取消")
        cb, handled = await click(ui, ALICE, cancel.callback_data)
        assert handled == "bu" and len(await ui.services.binding.list(ALICE)) == 1

    async def test_menu_shows_security_note_and_masked_bindings(self, ui):
        assert (await ui.services.binding.bind_binance(ALICE, API_KEY, API_SECRET)).ok
        cb, _ = await click(ui, ALICE, ui.signer(ALICE)("b"))
        text = shown(cb)[0]
        assert "只接受「唯讀」API 金鑰" in text and "助記詞" in text and "…AB12" in text and API_KEY not in text


# --------------------------------------------------------------------------- 10. alert buttons on live and simulated alerts


class TestAlertButtons:
    async def test_notifier_turns_alert_actions_into_ui_buttons(self, ui):
        bot = AsyncMock()
        notifier = TelegramNotifier(bot, ui.alert_markup)
        await notifier.notify(ALICE, "🚨", [("🛟 Rescue options", "rescue:binance:BTCUSDT"), ("✅ I'm awake", "ack:AbCdEf123_-x")])
        markup = bot.send_message.await_args.kwargs["reply_markup"]
        assert labels(markup) == ["🚑 先處理這個", "✅ 我醒了", "😴 暫停提醒 30 分鐘"]
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

    async def test_simulation_alerts_lead_with_ui_buttons(self, ctx, ui):
        bot = AsyncMock()
        sim = ctx.simulations.get_or_create(ALICE)
        TelegramSimulationBridge(bot, sim, ui.alert_markup)
        sim.set_scenario("cross_longs")
        await sim.start(source="test")
        await sim.wait()
        alerts = [c.kwargs["reply_markup"] for c in bot.send_message.await_args_list
                  if c.kwargs.get("reply_markup") and "🚑 先處理這個" in labels(c.kwargs["reply_markup"])]
        assert alerts
        with_ack = [m for m in alerts if "😴 暫停提醒 30 分鐘" in labels(m)]
        assert with_ack and all("✅ 我醒了" in labels(m) for m in with_ack)
        for markup in alerts:
            assert not any(b.callback_data in (None, "") for b in kb.iter_buttons(markup))
            assert not any((b.callback_data or "").startswith("ack:") for b in kb.iter_buttons(markup))

    async def test_live_snooze_holds_reminders(self, ctx, ui):
        now = time.time()
        ack = ctx.escalations.open(ALICE, "binance", "BTCUSDT", "critical", "x", now - 120, 60, 5)
        assert ack in ctx.escalations.due(now)
        await click(ui, ALICE, ui.signer(ALICE)("az", ack.id))
        assert ack not in ctx.escalations.due(now + 60)
        assert ack in ctx.escalations.due(now + 31 * 60)
