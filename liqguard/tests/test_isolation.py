"""Multi-user isolation: user A must never see or receive user B's data."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from liqguard.ai import MetaLLMClient
from liqguard.alerts import AlertManager, PositionMonitor
from liqguard.models import (
    TESTNET,
    Alert,
    AlertSeverity,
    AlertType,
    ExchangeBinding,
    Position,
    Side,
    User,
    UserThreshold,
    Venue,
)
from liqguard.protect import (
    ActionStatus,
    AuditLogEntry,
    AuditLogger,
    ExecutionTier,
    ProtectiveActionConfig,
    ProtectiveActionsManager,
)
from liqguard.risk import ActionableAdvice, ActionType
from liqguard.storage import EncryptionKeyError
from liqguard.web import create_app
from liqguard.web.auth import SESSION_COOKIE

from .conftest import ALICE, BOB

CSRF = {"X-LiqGuard-CSRF": "1"}


def position(network: str = "mainnet", distance_pct: float = 4.0) -> Position:
    mark = 100.0
    return Position(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        side=Side.LONG,
        size=1.0,
        entry_price=105.0,
        mark_price=mark,
        liquidation_price=mark * (1 - distance_pct / 100),
        margin=10.0,
        leverage=10.0,
        unrealized_pnl=-5.0,
        network=network,
    )


def alert(severity: AlertSeverity = AlertSeverity.WARNING) -> Alert:
    return Alert(
        alert_type=AlertType.DISTANCE_THRESHOLD,
        severity=severity,
        symbol="BTCUSDT",
        venue="binance",
        message="close to liquidation",
        distance_to_liquidation_pct=8.0,
        margin_to_add_for_safety=50.0,
    )


def advice(amount: float = 100.0) -> ActionableAdvice:
    return ActionableAdvice(
        action_type=ActionType.ADD_MARGIN,
        symbol="BTCUSDT",
        venue="binance",
        current_distance_pct=4.0,
        target_distance_pct=20.0,
        margin_to_add_usd=amount,
        estimated_cost_usd=amount,
    )


def audit_entry(user_id: int, action_id: str = "pa_x") -> AuditLogEntry:
    return AuditLogEntry(
        timestamp=datetime.now(timezone.utc),
        user_id=user_id,
        action_id=action_id,
        action_type=ActionType.ADD_MARGIN,
        symbol="BTCUSDT",
        venue="binance",
        status=ActionStatus.PROPOSED,
        amount_usd=10.0,
        details={},
    )


def enabled_manager(executor=None) -> ProtectiveActionsManager:
    return ProtectiveActionsManager(
        config=ProtectiveActionConfig(enabled=True, tier=ExecutionTier.TIER_1_CONFIRM),
        executor=executor,
    )


async def save_users(ctx) -> None:
    for uid in (ALICE, BOB):
        await ctx.storage.save_user(User(telegram_id=uid))


# --------------------------------------------------------------------------- storage


class TestStorageIsolation:
    async def test_bindings_are_scoped_to_owner(self, ctx):
        await ctx.storage.save_user(User(telegram_id=ALICE))
        await ctx.storage.save_user(User(telegram_id=BOB))
        await ctx.storage.save_binding(
            ALICE, ExchangeBinding(venue="binance", binding_type="api_key", api_key="A" * 64, api_secret="S" * 64)
        )

        alice = await ctx.storage.get_user(ALICE)
        bob = await ctx.storage.get_user(BOB)
        assert alice.get_binding("binance").api_key == "A" * 64
        assert bob.bindings == []

    async def test_ciphertext_cannot_be_decrypted_by_another_user(self, ctx):
        await save_users(ctx)
        token = await ctx.storage.encrypt_for_user(ALICE, "alice-secret")
        await ctx.storage.encrypt_for_user(BOB, "warm up bob's data key")

        assert await ctx.storage.decrypt_for_user(ALICE, token) == "alice-secret"
        with pytest.raises(EncryptionKeyError):
            await ctx.storage.decrypt_for_user(BOB, token)

    async def test_users_get_distinct_data_keys(self, ctx):
        await save_users(ctx)
        a = await ctx.storage.encrypt_for_user(ALICE, "same plaintext")
        b = await ctx.storage.encrypt_for_user(BOB, "same plaintext")
        assert a != b
        with pytest.raises(EncryptionKeyError):
            await ctx.storage.decrypt_for_user(ALICE, b)

    async def test_deleting_one_users_binding_leaves_the_other(self, ctx):
        for uid in (ALICE, BOB):
            await ctx.storage.save_user(User(telegram_id=uid))
            await ctx.storage.save_binding(uid, ExchangeBinding(venue="hyperliquid", binding_type="wallet", wallet_address=f"0x{uid}"))

        assert await ctx.storage.delete_binding(ALICE, "hyperliquid")
        assert (await ctx.storage.get_user(ALICE)).bindings == []
        assert (await ctx.storage.get_user(BOB)).get_binding("hyperliquid").wallet_address == f"0x{BOB}"

    async def test_audit_rows_are_per_user(self, ctx):
        await save_users(ctx)
        ctx.actions.audit.log(audit_entry(ALICE, "pa_alice"))
        ctx.actions.audit.log(audit_entry(BOB, "pa_bob"))

        alice_rows = await ctx.storage.list_audit(ALICE)
        assert [r["action_id"] for r in alice_rows] == ["pa_alice"]
        assert [r["action_id"] for r in await ctx.storage.list_audit(BOB)] == ["pa_bob"]

    async def test_revoking_one_user_keeps_other_sessions(self, ctx):
        await ctx.storage.save_user(User(telegram_id=ALICE))
        await ctx.storage.save_user(User(telegram_id=BOB))
        await ctx.storage.bump_session_epoch(ALICE)
        assert await ctx.storage.get_session_epoch(ALICE) == 1
        assert await ctx.storage.get_session_epoch(BOB) == 0


# --------------------------------------------------------------------------- alerts


class TestAlertIsolation:
    def test_same_symbol_does_not_share_cooldown(self):
        manager = AlertManager()
        assert manager.process_alert(ALICE, alert(), 300)
        assert manager.process_alert(ALICE, alert(), 300) is None
        assert manager.process_alert(BOB, alert(), 300)

    def test_history_and_stats_are_per_user(self):
        manager = AlertManager()
        manager.process_alert(ALICE, alert(), 0)
        assert len(manager.get_alert_history(ALICE, "binance", "BTCUSDT")) == 1
        assert manager.get_alert_history(BOB, "binance", "BTCUSDT") == []
        assert manager.get_stats(BOB)["total_alerts_sent"] == 0

    def test_clearing_one_user_keeps_the_other(self):
        manager = AlertManager()
        manager.process_alert(ALICE, alert(), 300)
        manager.process_alert(BOB, alert(), 300)
        manager.clear_user_state(ALICE)
        assert manager.process_alert(ALICE, alert(), 300)
        assert manager.process_alert(BOB, alert(), 300) is None

    async def test_monitor_notifies_only_the_position_owner(self):
        alice = User(telegram_id=ALICE, threshold=UserThreshold())
        bob = User(telegram_id=BOB, threshold=UserThreshold())
        books = {ALICE: [position(distance_pct=3.0)], BOB: []}

        async def users():
            return [alice, bob]

        async def positions(user):
            return books[user.telegram_id]

        notifier = AsyncMock()
        monitor = PositionMonitor(users, positions, notifier, AlertManager(), MetaLLMClient(use_mock=True))
        await monitor.check_all_users()

        recipients = {c.args[0] for c in notifier.notify.call_args_list}
        assert recipients == {ALICE}


# --------------------------------------------------------------------------- simulations


class TestSimulationIsolation:
    async def test_each_user_gets_their_own_simulation(self, ctx):
        a = ctx.simulations.get_or_create(ALICE)
        b = ctx.simulations.get_or_create(BOB)
        assert a is not b
        assert a.owner_id == ALICE and b.owner_id == BOB
        assert ctx.simulations.public.owner_id is None
        assert ctx.simulations.get_or_create(ALICE) is a

    async def test_simulation_alerts_reach_only_the_requester(self, ctx):
        from liqguard.telegram import BotHandlers, attach_bridges

        from .conftest import fake_message

        bot = AsyncMock()
        ctx.simulations.on_create(attach_bridges(bot))
        handlers = BotHandlers(ctx)
        await ctx.storage.save_user(User(telegram_id=BOB))

        await handlers._start_liquidation_simulation(ALICE, fake_message(ALICE))
        await ctx.simulations.get(ALICE).wait()

        recipients = {c.args[0] for c in bot.send_message.call_args_list}
        assert recipients == {ALICE}
        assert ctx.simulations.get(BOB) is None
        assert ctx.simulations.public.alerts == []

    async def test_public_demo_never_reaches_telegram(self, ctx):
        from liqguard.telegram import attach_bridges

        bot = AsyncMock()
        ctx.simulations.on_create(attach_bridges(bot))
        await ctx.simulations.public.start(source="web")
        await ctx.simulations.public.wait()
        bot.send_message.assert_not_called()

    async def test_audit_mirrors_only_into_the_owners_simulation(self, ctx):
        a = ctx.simulations.get_or_create(ALICE)
        b = ctx.simulations.get_or_create(BOB)
        ctx.actions.audit.log(audit_entry(ALICE))
        await a.wait()
        await b.wait()
        assert len(a.audit) == 1
        assert b.audit == []
        assert ctx.simulations.public.audit == []

    async def test_demo_books_are_per_user(self, ctx):
        ctx.positions.demo_adapter(ALICE).add_custom_position("DOGEUSDT", Side.LONG, 1000, 0.1, 0.1, 10, 10)
        bob_symbols = {p.symbol for p in await ctx.positions.demo_adapter(BOB).get_positions()}
        alice_symbols = {p.symbol for p in await ctx.positions.demo_adapter(ALICE).get_positions()}
        assert "DOGEUSDT" in alice_symbols
        assert "DOGEUSDT" not in bob_symbols


# --------------------------------------------------------------------------- protective actions


class TestProtectiveActionIsolation:
    def test_pending_actions_are_invisible_to_other_users(self):
        manager = enabled_manager()
        action, _ = manager.propose_action(ALICE, advice(), position(TESTNET))
        assert [a.action_id for a in manager.get_pending_actions(ALICE)] == [action.action_id]
        assert manager.get_pending_actions(BOB) == []

    async def test_other_user_cannot_confirm_execute_or_cancel(self):
        executor = AsyncMock()
        executor.execute.return_value = (True, "ok")
        manager = enabled_manager(executor)
        action, _ = manager.propose_action(ALICE, advice(), position(TESTNET))

        assert manager.confirm_action(action.action_id, BOB) == (False, "Action not found.")
        assert manager.cancel_action(action.action_id, BOB)[0] is False
        manager.confirm_action(action.action_id, ALICE)
        assert await manager.execute_action(action.action_id, BOB) == (False, "Action not found.")
        executor.execute.assert_not_called()

        assert (await manager.execute_action(action.action_id, ALICE))[0] is True
        executor.execute.assert_awaited_once()

    def test_kill_switch_is_per_user(self):
        manager = enabled_manager()
        bob_action, _ = manager.propose_action(BOB, advice(), position(TESTNET))
        manager.activate_kill_switch(ALICE)
        assert manager.is_kill_switch_active(ALICE)
        assert not manager.is_kill_switch_active(BOB)
        assert bob_action.status == ActionStatus.PROPOSED

    async def test_daily_limits_are_per_user(self):
        executor = AsyncMock()
        executor.execute.return_value = (True, "ok")
        manager = enabled_manager(executor)
        limit = manager.config.limits.max_daily_total_usd
        amount = manager.config.limits.max_single_action_usd
        for _ in range(int(limit // amount)):
            action, _ = manager.propose_action(ALICE, advice(amount), position(TESTNET))
            manager.confirm_action(action.action_id, ALICE)
            await manager.execute_action(action.action_id, ALICE)

        assert manager.propose_action(ALICE, advice(amount), position(TESTNET))[0] is None
        assert manager.propose_action(BOB, advice(amount), position(TESTNET))[0] is not None

    async def test_mainnet_positions_are_never_executed(self):
        executor = AsyncMock()
        manager = enabled_manager(executor)
        action, _ = manager.propose_action(ALICE, advice(), position("mainnet"))
        manager.confirm_action(action.action_id, ALICE)
        ok, _ = await manager.execute_action(action.action_id, ALICE)
        assert not ok
        executor.execute.assert_not_called()

    def test_audit_subscribers_only_see_their_user(self):
        audit = AuditLogger()
        seen = []
        audit.subscribe(ALICE, seen.append)
        audit.log(audit_entry(BOB))
        audit.log(audit_entry(ALICE))
        assert [e.user_id for e in seen] == [ALICE]
        assert [e.user_id for e in audit.get_user_entries(BOB)] == [BOB]


# --------------------------------------------------------------------------- dashboard


@pytest.fixture
def dashboard(ctx):
    with TestClient(create_app(ctx), base_url="https://testserver") as client:
        yield client


def sign_in(client: TestClient, ctx, user_id: int) -> None:
    client.cookies.set(SESSION_COOKIE, ctx.sessions.issue(user_id, 0))


class TestDashboardIsolation:
    async def test_account_shows_only_the_session_user(self, ctx, dashboard):
        await ctx.storage.save_user(User(telegram_id=ALICE, username="alice"))
        await ctx.storage.save_user(User(telegram_id=BOB, username="bob"))
        await ctx.storage.save_binding(BOB, ExchangeBinding(venue="hyperliquid", binding_type="wallet", wallet_address="0xb0b"))
        ctx.actions.audit.log(audit_entry(BOB, "pa_bob_only"))

        sign_in(dashboard, ctx, ALICE)
        body = dashboard.get("/api/me/account").json()
        assert body["bindings"] == []
        assert body["actions"] == []
        assert "0xb0b" not in str(body) and "pa_bob_only" not in str(body)
        assert dashboard.get("/api/me/session").json()["user_id"] == ALICE

    async def test_simulations_started_on_web_stay_private(self, ctx, dashboard):
        await save_users(ctx)
        sign_in(dashboard, ctx, ALICE)
        assert dashboard.post("/api/me/simulate", headers=CSRF).status_code == 200
        sim = ctx.simulations.get(ALICE)
        for _ in range(400):
            if sim.state == "finished":
                break
            await asyncio.sleep(0.05)

        assert dashboard.get("/api/me/alerts").json()
        assert ctx.simulations.get(BOB) is None
        assert dashboard.get("/api/demo/alerts").json() == []

        sign_in(dashboard, ctx, BOB)
        assert dashboard.get("/api/me/alerts").json() == []

    def test_me_api_requires_a_session(self, dashboard):
        for path in ("/api/me/account", "/api/me/snapshot", "/api/me/alerts", "/api/me/audit"):
            assert dashboard.get(path).status_code == 401
        assert dashboard.post("/api/me/simulate", headers=CSRF).status_code == 401

    async def test_websocket_streams_only_the_session_users_simulation(self, ctx, dashboard):
        await ctx.storage.save_user(User(telegram_id=ALICE))
        sign_in(dashboard, ctx, ALICE)
        ctx.simulations.get_or_create(BOB)
        with dashboard.websocket_connect("/ws/me", headers={"origin": "https://testserver"}) as ws:
            first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert first["data"]["scope"] == "private"
        assert ctx.simulations.get(ALICE) is not None
