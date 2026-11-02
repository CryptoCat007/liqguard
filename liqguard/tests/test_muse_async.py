"""
Muse never holds up a message: Telegram replies, alerts and demo steps go out
with the deterministic text at once and are updated in place when Muse answers.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from liqguard.ai import MetaLLMClient
from liqguard.alerts import AlertManager, PositionMonitor
from liqguard.core import MuseService
from liqguard.models import Position, Side, User, Venue
from liqguard.simulation import MarketSimulation
from liqguard.telegram.handlers import BotHandlers
from liqguard.telegram.sim_bridge import TelegramSimulationBridge
from liqguard.web import create_app

from .conftest import ALICE, fake_callback, fake_message, until
from .test_muse_api import completion


class SmartMuse:
    """A valid answer for every prompt LiqGuard sends, held back until `gate` opens."""

    def __init__(self, gate: asyncio.Event = None, delay: float = 0.0):
        self.gate = gate
        self.delay = delay
        self.calls = 0
        self.prompts: list[str] = []

    def answer(self, system: str, user: str):
        if "rescue options" in system:
            plan = json.loads(user)
            return {"headline": "Muse compares the rescue options.", "recommended_option": plan["recommended"],
                    "trade_offs": [{"option": plan["recommended"], "summary": "Muse prefers this.", "pros": [], "cons": []}],
                    "caveat": "Muse caveat."}
        if "stop-loss" in system:
            return {"headline": "Muse reviewed the stop.", "explanation": "Muse explains the stop.", "suggested_fix": "none"}
        if "Summarize portfolio" in system or "總結投資組合" in system:
            return {"headline": f"Muse: {json.loads(user)['riskiest_symbol']} leads the risk."}
        if "Explain risk" in system or "解釋風險" in system:
            return {"summary": "Muse summary.", "explanation": "Muse explanation."}
        return None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(request.content)
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        self.prompts.append(system)
        data = self.answer(system, user if isinstance(user, str) else "")
        content = json.dumps(data) if data is not None else "Muse answer."
        return httpx.Response(200, json=completion(content))


def live_llm(muse: SmartMuse, timeout: float = 30) -> MetaLLMClient:
    return MetaLLMClient(api_key="sk-test", use_mock=False, timeout=timeout,
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(muse)))


def demo(llm, owner_id=None) -> MarketSimulation:
    return MarketSimulation(llm, owner_id=owner_id, tick_interval=0, escalation_seconds_per_minute=0.01)


async def collect(sim):
    events = []

    async def on(e):
        events.append(e)

    sim.subscribe("test", on)
    return events


class TestDemo:
    async def test_steps_run_on_while_muse_thinks_then_cards_update(self):
        gate = asyncio.Event()
        llm = live_llm(SmartMuse(gate))
        sim = demo(llm)
        events = await collect(sim)
        await sim.start()
        assert await until(lambda: sim.step == sim.steps, timeout=20)
        first = [e["data"] for e in events if e["type"] == "advice"]
        assert first and all(c["mock"] is True for c in first)
        assert sim.snapshot()["muse_mode"] == "pending"
        assert not any(e["type"] == "advice_update" for e in events)

        gate.set()
        await sim.wait()
        updates = [e["data"] for e in events if e["type"] == "advice_update"]
        assert {u["id"] for u in updates} == {c["id"] for c in first}
        assert all(u["mock"] is False and u["explanation"] == "Muse explanation." for u in updates)
        assert all(u["rescue_explanation"]["headline"] == "Muse compares the rescue options." for u in updates)
        assert [c["explanation"] for c in sim.advice] == ["Muse explanation."] * len(sim.advice)
        assert {"type": "muse", "data": {"mode": "live"}} in events
        assert sim.snapshot()["muse_mode"] == "live"

    async def test_muse_timeout_keeps_rule_based_text_and_says_so(self):
        sim = demo(live_llm(SmartMuse(asyncio.Event()), timeout=0.2))
        events = await collect(sim)
        await sim.start()
        await sim.wait()
        assert sim.state == "finished" and sim.advice
        assert all(c["mock"] is True for c in sim.advice)
        assert not any(e["type"] == "advice_update" for e in events)
        assert sim.snapshot()["muse_mode"] == "fallback"
        assert {"type": "muse", "data": {"mode": "fallback"}} in events

    async def test_stop_alert_is_updated_in_place(self):
        gate = asyncio.Event()
        sim = demo(live_llm(SmartMuse(gate)))
        events = await collect(sim)
        await sim.start()
        assert await until(lambda: sim.step == sim.steps, timeout=20)
        stops = [e["data"] for e in events if e["type"] == "alert" and e["data"]["level"] == "stop"]
        if not stops:
            pytest.skip("default scenario has no stop-loss alert")
        assert stops[0]["explanation"]["headline"] != "Muse reviewed the stop."
        gate.set()
        await sim.wait()
        updated = [e["data"] for e in events if e["type"] == "alert_update"]
        assert updated and updated[0]["id"] == stops[0]["id"]
        assert updated[0]["explanation"]["headline"] == "Muse reviewed the stop."

    def test_web_rescue_answers_now_and_with_wait(self):
        muse = SmartMuse()
        sim = demo(live_llm(muse))
        with TestClient(create_app(demo_simulation=sim)) as c:
            symbol = next(iter(c.get("/api/demo/snapshot").json()["history"]))
            now = c.get(f"/api/demo/rescue?symbol={symbol}").json()
            assert now["muse_pending"] is True and now["explanation"]["mock"] is True
            later = c.get(f"/api/demo/rescue?symbol={symbol}&wait=1").json()
            assert later["muse_pending"] is False and later["explanation"]["mock"] is False
            cached = c.get(f"/api/demo/rescue?symbol={symbol}").json()
            assert cached["muse_pending"] is False and cached["explanation"]["mock"] is False
            assert sum("rescue options" in p for p in muse.prompts) == 1

    def test_snapshot_does_not_wait_for_the_summary(self):
        gate = asyncio.Event()
        sim = demo(live_llm(SmartMuse(gate)))
        with TestClient(create_app(demo_simulation=sim)) as c:
            snap = c.get("/api/demo/snapshot").json()
            assert snap["summary"]["mock"] is True and snap["muse_mode"] == "pending"
            c.portal.call(gate.set)


class TestTelegram:
    @pytest.fixture
    def gate(self):
        return asyncio.Event()

    @pytest.fixture
    def handlers(self, ctx, gate):
        h = BotHandlers(ctx)
        h.muse = MuseService(live_llm(SmartMuse(gate)))
        return h

    async def demo_user(self, ctx, handlers):
        await ctx.storage.save_user(User(telegram_id=ALICE, is_demo_mode=True))
        sim = handlers.simulation_for(await ctx.storage.get_user(ALICE))
        sim.step = 30
        sim.positions = sim._positions_at(sim.step)
        sim.overview = sim._analyze()
        return sim

    async def test_risk_replies_at_once_then_edits_in_muse(self, ctx, handlers, gate):
        await self.demo_user(ctx, handlers)
        message = fake_message(ALICE, "/risk")
        await asyncio.wait_for(handlers.cmd_risk(message), 2)
        text = message.answer.call_args.args[0]
        assert text.startswith("🧠 _Muse (mock):_")
        sent = message.answer.return_value
        sent.edit_text.assert_not_called()
        gate.set()
        assert await until(lambda: sent.edit_text.await_count == 1)
        edited = sent.edit_text.call_args.args[0]
        assert edited.startswith("🧠 _Muse:_ Muse:") and edited.split("\n", 2)[2] == text.split("\n", 2)[2]
        assert sent.edit_text.call_args.kwargs["reply_markup"] is message.answer.call_args.kwargs["reply_markup"]

    async def test_rescue_button_replies_at_once_then_edits(self, ctx, handlers, gate):
        await self.demo_user(ctx, handlers)
        cb = fake_callback(ALICE, "rescue:sim:BTCUSDT")
        await asyncio.wait_for(handlers.callback_rescue(cb), 2)
        assert "Muse (mock)" in cb.message.answer.call_args.args[0]
        gate.set()
        sent = cb.message.answer.return_value
        assert await until(lambda: sent.edit_text.await_count == 1)
        assert "Muse compares the rescue options." in sent.edit_text.call_args.args[0]

    async def test_ask_shows_rule_based_answer_then_muse(self, ctx, handlers, gate):
        await self.demo_user(ctx, handlers)
        message = fake_message(ALICE, "/ask what is my risk?")
        await asyncio.wait_for(handlers.cmd_ask(message), 2)
        status = message.answer.return_value
        assert status.edit_text.await_count == 1 and "riskiest" in status.edit_text.call_args.args[0]
        gate.set()
        assert await until(lambda: status.edit_text.await_count == 2)
        assert status.edit_text.call_args.args[0] == "Muse answer."


class TestMonitor:
    async def test_alert_goes_out_at_once_and_is_edited_when_muse_answers(self):
        gate = asyncio.Event()
        user = User(telegram_id=ALICE)
        near = Position(Venue.BINANCE, "BTCUSDT", Side.LONG, 0.5, 65000, 60000, 58200, 3000, 10, -2500)

        async def users():
            return [user]

        async def positions(_):
            return [near]

        sent = MagicMock()
        sent.edit_text = AsyncMock()
        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value=sent)
        monitor = PositionMonitor(users, positions, notifier, AlertManager(), live_llm(SmartMuse(gate)))
        await asyncio.wait_for(monitor.check_all_users(), 2)
        alert = next(c.args[1] for c in notifier.notify.call_args_list if "BTCUSDT" in c.args[1])
        assert "Muse explanation." not in alert
        gate.set()
        assert await until(lambda: sent.edit_text.await_count >= 1)
        assert any("Muse explanation." in c.args[0] for c in sent.edit_text.call_args_list)


class TestSimBridge:
    async def test_advice_update_edits_the_message_it_sent(self):
        sim = demo(MetaLLMClient(use_mock=True), owner_id=ALICE)
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=77))
        bot.edit_message_text = AsyncMock()
        TelegramSimulationBridge(bot, sim)
        await sim.start()
        await sim.wait()
        card = dict(sim.advice[0], explanation="Muse explanation.", mock=False)
        await sim._emit("advice_update", card)
        bot.edit_message_text.assert_awaited_once()
        call = bot.edit_message_text.call_args
        assert call.kwargs["message_id"] == 77 and call.kwargs["chat_id"] == ALICE
        assert "Muse explanation." in call.args[0] and call.kwargs["reply_markup"] is not None
