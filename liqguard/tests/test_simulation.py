"""Tests for the demo market simulation, its owner-only Telegram bridge and handler integration."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from liqguard.ai import MetaLLMClient
from liqguard.models import Side
from liqguard.protect import ActionStatus, AuditLogEntry, ProtectiveActionsManager
from liqguard.risk import ActionType
from liqguard.simulation import TIERS, MarketSimulation, price_at_distance
from liqguard.telegram.sim_bridge import TelegramSimulationBridge, format_advice_message

from .conftest import fake_message


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
            if spec.is_driver:
                assert pos.distance_to_liquidation_pct == pytest.approx(spec.end_distance_pct, abs=1e-6)
            else:
                assert pos.mark_price == pytest.approx(spec.entry_price * (1 + spec.end_price_pct / 100), rel=1e-9)

    def test_starts_safe_across_two_venues(self, sim):
        assert sim.overview.positions_at_risk == 0
        assert sim.overview.venues == ["binance", "hyperliquid"]


class TestAlerts:
    async def test_all_three_tiers_fire_in_order_once(self, sim):
        events = await run(sim)
        alerts = [e["data"] for e in events if e["type"] == "alert" and e["data"]["level"] in TIERS]
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
            if a["level"] in TIERS:
                assert a["distance_pct"] <= a["threshold_pct"]

    async def test_every_alert_gets_advice_with_code_numbers(self, sim):
        await run(sim)
        assert len(sim.advice) == len([a for a in sim.alerts if a["level"] in TIERS])
        for card in sim.advice:
            assert card["margin_to_add_usd"] > 0
            assert card["reduce_size_pct"] > 0
            assert card["mock"] is True
            assert card["explanation"]

    async def test_skipped_tiers_are_backfilled(self):
        sim = MarketSimulation(None, tick_interval=0, steps=2)
        events = await run(sim)
        btc = [e["data"]["level"] for e in events
               if e["type"] == "alert" and e["data"]["symbol"] == "BTCUSDT" and e["data"]["level"] in TIERS]
        assert btc == list(TIERS)

    async def test_alert_lists_delivery_channels(self, sim):
        sim.subscribe("web", AsyncMock())
        await run(sim)
        assert {"web", "test"} <= set(sim.alerts[0]["channels"])

    async def test_summary_names_riskiest(self, sim):
        await run(sim)
        assert sim.summary["riskiest_symbol"] == sim.overview.riskiest_position.symbol
        assert sim.summary["riskiest_symbol"] in sim.summary["headline"]

    async def test_summary_reflects_final_tick(self, sim):
        await run(sim)
        final = sim.overview.riskiest_position.distance_to_liquidation_pct
        assert sim.summary["min_distance_pct"] == pytest.approx(final, abs=0.01)

    async def test_reset_emits_fresh_summary(self, sim):
        events = await run(sim)
        events.clear()
        await sim.reset_and_notify()
        assert [e["type"] for e in events] == ["reset", "summary"]
        assert sim.summary["min_distance_pct"] > 20


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


def audit_entry(user_id: int) -> AuditLogEntry:
    return AuditLogEntry(
        timestamp=datetime.now(timezone.utc),
        user_id=user_id,
        action_id="pa_1",
        action_type=ActionType.ADD_MARGIN,
        symbol="BTCUSDT",
        venue="binance",
        status=ActionStatus.REJECTED,
        amount_usd=1200.0,
        details={"reason": "Protective actions are disabled.", "position": {"secret": "x"}},
    )


class TestAuditMirroring:
    async def test_owner_entries_are_mirrored_sanitized(self):
        manager = ProtectiveActionsManager()
        sim = MarketSimulation(None, owner_id=987654321, tick_interval=0)
        manager.audit.subscribe(987654321, sim.record_protective_audit)

        manager.audit.log(audit_entry(987654321))
        await sim.wait()

        entry = sim.audit[-1]
        assert entry["event"] == "action_rejected"
        assert entry["source"] == "telegram"
        assert "987654321" not in str(entry)
        assert "secret" not in str(entry)
        assert "$1,200.00" in entry["detail"]

    async def test_other_users_entries_are_not_mirrored(self):
        manager = ProtectiveActionsManager()
        sim = MarketSimulation(None, owner_id=1, tick_interval=0)
        manager.audit.subscribe(1, sim.record_protective_audit)
        manager.audit.log(audit_entry(2))
        sim.record_protective_audit(audit_entry(2))
        await sim.wait()
        assert sim.audit == []


class TestTelegramBridge:
    async def test_run_reaches_only_the_owner(self):
        sim = MarketSimulation(MetaLLMClient(use_mock=True), owner_id=42, tick_interval=0)
        bot = AsyncMock()
        TelegramSimulationBridge(bot, sim)

        await sim.start(source="web")
        await sim.wait()

        calls = bot.send_message.call_args_list
        assert calls and {c.args[0] for c in calls} == {42}
        texts = [c.args[1] for c in calls]
        assert any("simulation started" in t for t in texts)
        alert_texts = [t for t in texts if "ALERT" in t]
        assert len(alert_texts) == len(sim.alerts)
        assert any("EMERGENCY ALERT" in t for t in alert_texts)
        assert "telegram" in sim.alerts[0]["channels"]

    def test_public_simulation_cannot_be_bridged(self, sim):
        with pytest.raises(ValueError):
            TelegramSimulationBridge(AsyncMock(), sim)

    async def test_unsubscribe_stops_delivery(self):
        sim = MarketSimulation(None, owner_id=7, tick_interval=0)
        bot = AsyncMock()
        TelegramSimulationBridge(bot, sim).unsubscribe()
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
    def bot(self, ctx):
        from liqguard.telegram import attach_bridges

        bot = AsyncMock()
        ctx.simulations.on_create(attach_bridges(bot))
        return bot

    @pytest.fixture
    def handlers(self, ctx, bot):
        from liqguard.telegram import BotHandlers

        return BotHandlers(ctx)

    async def test_telegram_demo_starts_the_users_own_simulation(self, handlers, ctx, bot):
        await handlers._start_liquidation_simulation(555, fake_message(555))
        sim = ctx.simulations.get(555)
        await sim.wait()

        assert sim.owner_id == 555 and sim.state == "finished"
        assert ctx.simulations.public.state == "idle"
        assert any(e["event"] == "simulation_started" and e["source"] == "telegram" for e in sim.audit)
        assert {c.args[0] for c in bot.send_message.call_args_list} == {555}

    async def test_positions_follow_own_simulation(self, handlers, ctx):
        from liqguard.models import User

        await ctx.storage.save_user(User(telegram_id=7, is_demo_mode=True))
        sim = ctx.simulations.get_or_create(7)
        await sim.start()
        await sim.wait()
        user = await ctx.storage.get_user(7)

        positions, label = await handlers._get_positions_for_user(user)

        assert [p.mark_price for p in positions] == [p.mark_price for p in sim.positions]
        assert "simulation" in label

    async def test_simulation_uses_owner_thresholds(self, handlers, ctx):
        from liqguard.models import User, UserThreshold

        await ctx.storage.save_user(User(telegram_id=8, threshold=UserThreshold(warning_distance_pct=25.0)))
        await handlers._start_liquidation_simulation(8, fake_message(8))
        sim = ctx.simulations.get(8)
        await sim.wait()
        assert sim.snapshot()["thresholds"]["warning"] == 25.0

    async def test_risk_text_has_muse_headline(self, handlers, sim):
        text = await handlers._risk_overview_text(sim.overview, "en")
        assert text.startswith("🧠 _Muse (mock):_")
        assert sim.overview.riskiest_position.symbol in text.splitlines()[0]
