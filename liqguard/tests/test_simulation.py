"""Tests for the shared demo market simulation and its Telegram bridge."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from liqguard.llm import MetaLLMClient
from liqguard.models import Side
from liqguard.services.advice import ActionType
from liqguard.services.protective_actions import (
    ActionStatus,
    AuditLogEntry,
    ProtectiveActionsManager,
)
from liqguard.services.simulation import TIERS, MarketSimulation, price_at_distance
from liqguard.telegram.sim_bridge import (
    TelegramSimulationBridge,
    format_advice_message,
    parse_chat_ids,
)


@pytest.fixture
def sim():
    return MarketSimulation(MetaLLMClient(use_mock=True), tick_interval=0)


async def run(sim: MarketSimulation) -> list[dict]:
    events: list[dict] = []

    async def collect(e):
        events.append(e)

    sim.subscribe("test", collect)
    assert await sim.start()
    await sim.wait()
    return events


class TestPricePath:
    @pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
    def test_price_at_distance_roundtrip(self, side):
        from liqguard.models import Position, Venue
        liq = 50000.0
        mark = price_at_distance(side, liq, 12.5)
        pos = Position(Venue.DEMO, "X", side, 1, mark, mark, liq, 1, 1, 0)
        assert pos.distance_to_liquidation_pct == pytest.approx(12.5)

    def test_path_is_deterministic(self):
        a = MarketSimulation(None, tick_interval=0, seed=3)
        b = MarketSimulation(None, tick_interval=0, seed=3)
        for step in (0, 10, 25, 40):
            assert [p.mark_price for p in a._positions_at(step)] == [p.mark_price for p in b._positions_at(step)]

    def test_path_ends_at_configured_distance(self, sim):
        final = sim._positions_at(sim.steps)
        for spec, pos in zip(sim.specs, final):
            assert pos.distance_to_liquidation_pct == pytest.approx(spec.end_distance_pct, abs=1e-6)

    def test_starts_safe_across_two_venues(self, sim):
        assert sim.overview.positions_at_risk == 0
        assert sim.overview.venues == ["binance", "hyperliquid"]


class TestAlerts:
    async def test_all_three_tiers_fire_in_order_once(self, sim):
        events = await run(sim)
        alerts = [e["data"] for e in events if e["type"] == "alert"]
        levels = {a["level"] for a in alerts}
        assert levels == set(TIERS)

        keys = [(a["symbol"], a["level"]) for a in alerts]
        assert len(keys) == len(set(keys))

        for symbol in {a["symbol"] for a in alerts}:
            seq = [a["level"] for a in alerts if a["symbol"] == symbol]
            assert seq == list(TIERS[: len(seq)])

    async def test_alert_distance_within_threshold(self, sim):
        await run(sim)
        for a in sim.alerts:
            assert a["distance_pct"] <= a["threshold_pct"]

    async def test_every_alert_gets_advice_with_code_numbers(self, sim):
        await run(sim)
        assert len(sim.advice) == len(sim.alerts)
        for card in sim.advice:
            assert card["margin_to_add_usd"] > 0
            assert card["reduce_size_pct"] > 0
            assert card["mock"] is True
            assert card["explanation"]

    async def test_skipped_tiers_are_backfilled(self):
        sim = MarketSimulation(None, tick_interval=0, steps=2)
        events = await run(sim)
        btc = [e["data"]["level"] for e in events if e["type"] == "alert" and e["data"]["symbol"] == "BTCUSDT"]
        assert btc == list(TIERS)

    async def test_alert_lists_delivery_channels(self, sim):
        sim.subscribe("web", AsyncMock())
        await run(sim)
        assert {"web", "test"} <= set(sim.alerts[0]["channels"])

    async def test_summary_names_riskiest(self, sim):
        await run(sim)
        assert sim.summary["riskiest_symbol"] == sim.overview.riskiest_position.symbol
        assert sim.summary["riskiest_symbol"] in sim.summary["headline"]


class TestControl:
    async def test_double_start_is_rejected(self):
        sim = MarketSimulation(None, tick_interval=0.05)
        assert await sim.start()
        assert not await sim.start()
        await sim.stop()
        assert sim.state == "stopped"

    async def test_restart_resets_history(self, sim):
        await run(sim)
        first = len(sim.alerts)
        await sim.start()
        await sim.wait()
        assert len(sim.alerts) == first
        assert len(sim.history["BTCUSDT"]) == sim.steps + 1

    async def test_failing_subscriber_does_not_break_run(self, sim):
        sim.subscribe("broken", AsyncMock(side_effect=RuntimeError("boom")))
        await run(sim)
        assert sim.state == "finished"

    def test_snapshot_has_no_secrets(self):
        llm = MetaLLMClient(api_key="sk-super-secret-value", use_mock=True)
        snap = MarketSimulation(llm, tick_interval=0).snapshot()
        assert "sk-super-secret-value" not in str(snap)
        assert snap["demo"] is True


class TestAuditMirroring:
    async def test_protective_action_audit_is_mirrored_without_user_id(self, tmp_path):
        manager = ProtectiveActionsManager()
        manager.audit.log_path = tmp_path / "audit.log"
        sim = MarketSimulation(None, actions_manager=manager, tick_interval=0)

        manager.audit.log(AuditLogEntry(
            timestamp=datetime.now(timezone.utc),
            user_id=987654321,
            action_id="pa_1",
            action_type=ActionType.ADD_MARGIN,
            symbol="BTCUSDT",
            venue="binance",
            status=ActionStatus.REJECTED,
            amount_usd=1200.0,
            details={"reason": "Protective actions are disabled.", "position": {"secret": "x"}},
        ))
        await sim.wait()

        entry = sim.audit[-1]
        assert entry["event"] == "action_rejected"
        assert entry["source"] == "telegram"
        assert "987654321" not in str(entry)
        assert "secret" not in str(entry)
        assert "$1,200.00" in entry["detail"]


class TestTelegramBridge:
    def test_parse_chat_ids(self):
        assert parse_chat_ids("123, -100456 ,abc,,") == {123, -100456}
        assert parse_chat_ids(None) == set()

    async def test_web_triggered_run_reaches_telegram(self, sim):
        bot = AsyncMock()
        bridge = TelegramSimulationBridge(bot, sim, chat_ids=[42])
        assert bridge.has_chat(42)

        await sim.start(source="web")
        await sim.wait()

        texts = [c.args[1] for c in bot.send_message.call_args_list]
        assert all(c.args[0] == 42 for c in bot.send_message.call_args_list)
        assert any("simulation started" in t for t in texts)
        alert_texts = [t for t in texts if "ALERT" in t]
        assert len(alert_texts) == len(sim.alerts)
        assert any("EMERGENCY ALERT" in t for t in alert_texts)
        assert "telegram" in sim.alerts[0]["channels"]

    async def test_no_chats_sends_nothing(self, sim):
        bot = AsyncMock()
        TelegramSimulationBridge(bot, sim)
        await sim.start()
        await sim.wait()
        bot.send_message.assert_not_called()

    def test_format_advice_message(self):
        text = format_advice_message({
            "level": "critical", "symbol": "ETH", "side": "short", "venue": "hyperliquid",
            "distance_pct": 9.4, "mark_price": 3565.2, "liquidation_price": 3900.0,
            "explanation": "Elevated risk.", "margin_to_add_usd": 4321.5,
            "target_distance_pct": 30.0, "reduce_size_pct": 68.6, "mock": True,
        })
        assert "CRITICAL ALERT" in text
        assert "$4,321.50" in text
        assert "68.6%" in text
        assert "Muse (mock)" in text


class TestTelegramHandlersIntegration:
    @pytest.fixture
    def handlers(self, sim):
        from liqguard.services import AlertManager, RiskEngine
        from liqguard.telegram import BotHandlers

        storage = AsyncMock()
        bridge = TelegramSimulationBridge(AsyncMock(), sim)
        return BotHandlers(
            storage=storage, risk_engine=RiskEngine(), alert_manager=AlertManager(),
            llm_client=sim.llm, simulation=sim, sim_bridge=bridge,
        )

    async def test_telegram_demo_starts_shared_simulation(self, handlers, sim):
        message = AsyncMock()
        await handlers._start_liquidation_simulation(555, message)
        await sim.wait()

        assert handlers.sim_bridge.has_chat(555)
        assert sim.state == "finished"
        assert any(e["event"] == "simulation_started" and e["source"] == "telegram" for e in sim.audit)
        sent_to = {c.args[0] for c in handlers.sim_bridge.bot.send_message.call_args_list}
        assert sent_to == {555}

    async def test_positions_follow_simulation_for_subscribed_demo_user(self, handlers, sim):
        from liqguard.models import User

        handlers.sim_bridge.add_chat(7)
        await sim.start()
        await sim.wait()
        user = User(telegram_id=7, is_demo_mode=True)

        positions, label = await handlers._get_positions_for_user(user)

        assert [p.mark_price for p in positions] == [p.mark_price for p in sim.positions]
        assert "simulation" in label

    async def test_risk_text_has_muse_headline(self, handlers, sim):
        text = await handlers._risk_overview_text(sim.overview, "en")
        assert text.startswith("🧠 _Muse (mock):_")
        assert sim.overview.riskiest_position.symbol in text.splitlines()[0]
