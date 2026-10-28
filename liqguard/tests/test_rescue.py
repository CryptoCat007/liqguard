"""Rescue simulator (add margin / reduce / hedge), Muse's explanation of it, and where it surfaces: sim, Telegram, dashboard."""

import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from liqguard.ai import MetaLLMClient, mock_rescue_explanation, validate_rescue_explanation
from liqguard.alerts import AlertManager, PositionMonitor
from liqguard.core import BookSnapshot
from liqguard.models import (
    CROSS,
    ISOLATED,
    FundingInfo,
    MaintenanceBracket,
    MarginAccount,
    Position,
    Side,
    User,
    UserThreshold,
    Venue,
)
from liqguard.risk import (
    ADD_MARGIN,
    HEDGE,
    REDUCE,
    FeeSchedule,
    RescueSimulator,
    RiskContext,
    true_liquidation,
)
from liqguard.risk.rescue import RescueOption, recommend
from liqguard.simulation import TIERS, MarketSimulation
from liqguard.web import create_app
from liqguard.web.auth import SESSION_COOKIE

from .conftest import ALICE, BOB, fake_callback, fake_message

NOW = datetime(2026, 10, 27, 7, 40, tzinfo=timezone.utc)
FLAT_04 = (MaintenanceBracket(0, float("inf"), 0.004, 0),)
FEES = FeeSchedule({"binance": 0.0005, "hyperliquid": 0.00045, "demo": 0.0005})
CSRF = {"X-LiqGuard-CSRF": "1"}


def position(symbol="BTCUSDT", side=Side.LONG, size=0.8, entry=64200.0, mark=50000.0, mode=CROSS, venue=Venue.BINANCE, wallet=None):
    sign = 1 if side == Side.LONG else -1
    return Position(venue, symbol, side, size, entry, mark, 0.0, 10000, 5, sign * size * (mark - entry),
                    margin_mode=mode, isolated_wallet=wallet)


def cross_book():
    btc = position()
    sol = position("SOLUSDT", size=150, entry=148, mark=140)
    account = MarginAccount(Venue.BINANCE, 15500, [btc, sol], {"BTCUSDT": FLAT_04, "SOLUSDT": FLAT_04})
    funding = FundingInfo(Venue.BINANCE, "BTCUSDT", 0.0004, 8, NOW + timedelta(hours=3))
    return btc, account, funding


@pytest.fixture
def plan():
    btc, account, funding = cross_book()
    return RescueSimulator(FEES, 30).simulate(btc, account, funding, NOW)


class TestOptions:
    def test_starts_at_risk(self, plan):
        assert plan.current.distance_pct < 30 and plan.margin_mode == CROSS

    def test_add_margin_reaches_target_for_free(self, plan):
        o = plan.option(ADD_MARGIN)
        assert o.reaches_target and o.new_distance_pct == pytest.approx(30, abs=1e-6)
        assert o.fees_usd == 0 and o.realized_loss_usd == 0
        assert o.params["amount_usd"] > 0
        assert o.capital_required_usd == pytest.approx(o.params["amount_usd"])
        assert o.funding_24h_usd == pytest.approx(3 * 0.8 * 50000 * 0.0004)

    def test_reduce_realizes_pro_rata_loss_and_pays_taker_fee(self, plan):
        o = plan.option(REDUCE)
        f = o.params["reduce_pct"] / 100
        assert o.reaches_target and o.new_distance_pct == pytest.approx(30, abs=1e-4)
        assert o.realized_loss_usd == pytest.approx(f * 0.8 * (64200 - 50000))
        assert o.fees_usd == pytest.approx(f * 0.8 * 50000 * 0.0005)
        assert o.funding_24h_usd == pytest.approx(3 * (1 - f) * 0.8 * 50000 * 0.0004)

    def test_cross_reduce_new_liquidation_is_consistent(self, plan):
        btc, account, _ = cross_book()
        o = plan.option(REDUCE)
        f = o.params["reduce_pct"] / 100
        smaller = Position(btc.venue, btc.symbol, btc.side, btc.size * (1 - f), btc.entry_price, btc.mark_price, 0.0,
                           btc.margin, btc.leverage, btc.unrealized_pnl * (1 - f), margin_mode=CROSS)
        wallet = account.wallet_balance - o.realized_loss_usd - o.fees_usd
        after = true_liquidation(smaller, MarginAccount(Venue.BINANCE, wallet, [smaller, account.positions[1]], account.schedules))
        assert after.price == pytest.approx(o.new_liquidation_price, rel=1e-9)

    def test_binance_cross_hedge_shares_the_wallet(self, plan):
        o = plan.option(HEDGE)
        h = o.params["hedge_size"]
        assert o.protects_liquidation and o.reaches_target
        assert o.fees_usd == pytest.approx(2 * h * 50000 * 0.0005)
        assert o.realized_loss_usd == 0
        assert "Hedge Mode" in o.title

    def test_hyperliquid_hedge_does_not_move_liquidation(self):
        eth = position("ETH-PERP", Side.SHORT, size=12, entry=3150, mark=3700, venue=Venue.HYPERLIQUID)
        acct = MarginAccount(Venue.HYPERLIQUID, 11300, [eth], {"ETH": (MaintenanceBracket(0, float("inf"), 0.02, 0),)})
        p = RescueSimulator(FEES, 30).simulate(eth, acct, None, NOW)
        o = p.option(HEDGE)
        assert not o.protects_liquidation and not o.reaches_target
        assert o.new_liquidation_price == pytest.approx(p.current.price)
        assert o.params["hedge_venue"] == "binance"
        assert p.recommended != HEDGE

    def test_isolated_partial_close_barely_helps(self):
        p0 = position(mode=ISOLATED, mark=48000, wallet=8000)
        p = RescueSimulator(FEES, 30).simulate(p0, None, None, NOW)
        reduce = p.option(REDUCE)
        assert reduce.params["reduce_pct"] == pytest.approx(100)
        assert reduce.title.startswith("Close") and reduce.new_liquidation_price is None
        assert any("pro rata" in n for n in reduce.notes)
        assert p.option(ADD_MARGIN).reaches_target

    def test_short_receiving_funding_has_negative_funding_cost(self):
        eth = position("ETHUSDT", Side.SHORT, size=10, entry=3000, mark=3400, mode=ISOLATED, wallet=3600)
        info = FundingInfo(Venue.BINANCE, "ETHUSDT", 0.0002, 8, NOW + timedelta(hours=1))
        p = RescueSimulator(FEES, 30).simulate(eth, None, info, NOW)
        assert p.option(ADD_MARGIN).funding_24h_usd == pytest.approx(-3 * 10 * 3400 * 0.0002)

    def test_safe_position_needs_nothing(self):
        safe = position(mode=ISOLATED, mark=64000, wallet=40000)
        p = RescueSimulator(FEES, 30).simulate(safe, None, None, NOW)
        assert p.option(ADD_MARGIN).params["amount_usd"] == 0
        assert p.margin_advice().margin_to_add_usd == 0

    def test_margin_advice_carries_exact_numbers(self, plan):
        advice = plan.margin_advice()
        assert advice.margin_to_add_usd == pytest.approx(plan.option(ADD_MARGIN).params["amount_usd"])
        assert advice.reduce_size_pct == pytest.approx(plan.option(REDUCE).params["reduce_pct"])
        assert advice.new_distance_pct == pytest.approx(30, abs=1e-6)

    def test_to_dict_is_json_ready(self, plan):
        d = json.loads(json.dumps(plan.to_dict()))
        assert [o["kind"] for o in d["options"]] == [ADD_MARGIN, REDUCE, HEDGE]
        assert d["funding_label"] == "at current rate"
        assert set(d["options"][0]["costs"]) == {"fees_usd", "funding_24h_usd", "realized_loss_usd", "capital_required_usd", "total_cost_usd"}


class TestAlreadySafe:
    def safe_plan(self):
        btc, account, funding = cross_book()
        return RescueSimulator(FEES, 5).simulate(btc, account, funding, NOW)

    def test_flag(self, plan):
        assert not plan.already_safe and plan.to_dict()["already_safe"] is False
        safe = self.safe_plan()
        assert safe.already_safe and safe.to_dict()["already_safe"] is True

    def test_mock_says_no_rescue_needed_and_validates(self):
        safe = self.safe_plan()
        ex = mock_rescue_explanation(safe)
        assert "no rescue needed" in ex["headline"]
        assert validate_rescue_explanation(ex, safe) is None

    def test_telegram_shows_no_options(self):
        from liqguard.telegram.views import format_rescue_text, plan_summary_line, rescue_keyboard

        safe = self.safe_plan()
        data = safe.to_dict()
        text = format_rescue_text(data, mock_rescue_explanation(safe))
        assert "no rescue needed" in text and "Reduce" not in text
        assert rescue_keyboard("live", data, True).inline_keyboard == []
        assert plan_summary_line(safe) == ""


class TestRecommend:
    def opt(self, kind, cost, reaches=True, protects=True, distance=30.0):
        return RescueOption(kind, kind, 1.0, distance, reaches, protects, cost, 0, 0, 0)

    def test_cheapest_viable_wins(self):
        assert recommend((self.opt(ADD_MARGIN, 50), self.opt(REDUCE, 20), self.opt(HEDGE, 30))) == REDUCE

    def test_non_protecting_option_never_wins_on_price(self):
        assert recommend((self.opt(ADD_MARGIN, 50), self.opt(REDUCE, 90), self.opt(HEDGE, 0, protects=False))) == ADD_MARGIN

    def test_ties_follow_option_order(self):
        assert recommend((self.opt(ADD_MARGIN, 10), self.opt(REDUCE, 10), self.opt(HEDGE, 10))) == ADD_MARGIN

    def test_nothing_reaches_target_picks_furthest(self):
        options = (self.opt(ADD_MARGIN, 1, False, distance=12), self.opt(REDUCE, 1, False, distance=18), self.opt(HEDGE, 1, False, False, 40))
        assert recommend(options) == REDUCE


# --------------------------------------------------------------------------- Muse


def live_client(content: str) -> MetaLLMClient:
    client = MetaLLMClient(api_key="test", use_mock=False)
    reply = MagicMock()
    reply.choices = [MagicMock(message=MagicMock(content=content))]
    client.client = MagicMock()
    client.client.chat.completions.create = AsyncMock(return_value=reply)
    return client


class TestMuseExplanation:
    async def test_mock_is_deterministic_and_valid(self, plan):
        a = await MetaLLMClient(use_mock=True).explain_rescue(plan)
        b = await MetaLLMClient(use_mock=True).explain_rescue(plan)
        assert a == b and a["mock"] is True
        assert a["recommended_option"] == plan.recommended
        assert [t["option"] for t in a["trade_offs"]] == [ADD_MARGIN, REDUCE, HEDGE]
        assert "current rate" in a["caveat"]
        assert validate_rescue_explanation(a, plan) is None

    async def test_mock_traditional_chinese(self, plan):
        zh = mock_rescue_explanation(plan, "zh-TW")
        assert "建議" in zh["headline"] and validate_rescue_explanation(zh, plan) is None

    async def test_live_answer_used_when_numbers_match(self, plan):
        add = plan.option(ADD_MARGIN)
        good = {
            "headline": f"Add ${add.params['amount_usd']:,.2f} to the wallet.",
            "recommended_option": plan.recommended,
            "trade_offs": [{"option": ADD_MARGIN, "summary": "No fees.", "pros": ["Keeps exposure"], "cons": ["Needs capital"]}],
            "caveat": "Funding changes.",
        }
        result = await live_client(json.dumps(good)).explain_rescue(plan)
        assert result["mock"] is False and result["headline"] == good["headline"]

    @pytest.mark.parametrize("mutate", [
        lambda d: d.update(recommended_option="yolo"),
        lambda d: d.update(headline="Just add $123,456.78 and relax."),
        lambda d: d["trade_offs"].append({"option": "close_everything", "summary": "", "pros": [], "cons": []}),
        lambda d: d.update(trade_offs=[]),
    ])
    async def test_live_answer_rejected_when_it_invents(self, plan, mutate):
        bad = copy.deepcopy(mock_rescue_explanation(plan))
        bad.pop("mock")
        mutate(bad)
        result = await live_client(json.dumps(bad)).explain_rescue(plan)
        assert result["mock"] is True and result == mock_rescue_explanation(plan)

    async def test_live_garbage_falls_back(self, plan):
        assert (await live_client("not json").explain_rescue(plan))["mock"] is True


# --------------------------------------------------------------------------- simulation


async def run(sim: MarketSimulation) -> list[dict]:
    events = []

    async def collect(e):
        events.append(e)

    sim.subscribe("test", collect)
    await sim.start()
    await sim.wait()
    return events


class TestSimulation:
    async def test_cross_accounts_and_computed_liquidation(self):
        sim = MarketSimulation(None, tick_interval=0)
        btc = sim._position("BTCUSDT")
        assert btc.margin_mode == CROSS
        account = sim._account(btc)
        assert {p.symbol for p in account.positions} == {"BTCUSDT", "SOLUSDT"}
        assert btc.liquidation_price == pytest.approx(true_liquidation(btc, account).price)

    async def test_sol_is_dragged_by_btc_through_the_shared_wallet(self):
        sim = MarketSimulation(None, tick_interval=0)
        await run(sim)
        sol_tiers = [a["level"] for a in sim.alerts if a["symbol"] == "SOLUSDT" and a["level"] in TIERS]
        assert "critical" in sol_tiers
        spec = next(s for s in sim.specs if s.symbol == "SOLUSDT")
        assert abs(spec.end_price_pct) <= 5

    async def test_funding_settlements_debit_wallets_and_move_liquidation(self):
        sim = MarketSimulation(None, tick_interval=0)
        start = dict(sim.wallets)
        events = await run(sim)
        settlements = [e["data"] for e in events if e["type"] == "funding"]
        assert {s["symbol"] for s in settlements} == {"BTCUSDT", "SOLUSDT", "ETH"}
        assert sum(1 for s in settlements if s["symbol"] == "ETH") == 8  # hourly over 8 simulated hours
        paid = {v: sum(s["payment_usd"] for s in settlements if s["venue"] == v.value) for v in start}
        for venue, wallet in sim.wallets.items():
            assert wallet == pytest.approx(start[venue] - paid[venue], abs=0.05)
        btc = next(s for s in settlements if s["symbol"] == "BTCUSDT")
        assert btc["liquidation_after"] > btc["liquidation_before"]
        assert any(p.get("funding_paid_usd") for p in sim.history["BTCUSDT"])

    async def test_prefunding_alert_precedes_the_settlement(self):
        sim = MarketSimulation(None, tick_interval=0)
        events = await run(sim)
        warning = next(e["data"] for e in events if e["type"] == "alert" and e["data"]["level"] == "funding")
        settle = next(e["data"] for e in events if e["type"] == "funding" and e["data"]["symbol"] == warning["symbol"]
                      and e["data"]["sim_time"] == warning["funding"]["settlement_time"])
        assert warning["step"] < settle["step"]
        assert 0 < warning["funding"]["minutes_to_settlement"] <= 30
        assert warning["funding"]["distance_after_pct"] < warning["funding"]["distance_before_pct"]
        keys = [(a["symbol"], a["funding"]["settlement_time"]) for a in sim.alerts if a["level"] == "funding"]
        assert len(keys) == len(set(keys))

    async def test_advice_cards_carry_rescue_plans(self):
        sim = MarketSimulation(MetaLLMClient(use_mock=True), tick_interval=0)
        await run(sim)
        for card in sim.advice:
            rescue = card["rescue"]
            assert [o["kind"] for o in rescue["options"]] == [ADD_MARGIN, REDUCE, HEDGE]
            assert card["margin_to_add_usd"] == pytest.approx(rescue["options"][0]["params"]["amount_usd"], abs=0.01)
            assert card["rescue_explanation"]["recommended_option"] == rescue["recommended"]

    async def test_on_demand_plan_and_snapshot(self):
        sim = MarketSimulation(MetaLLMClient(use_mock=True), tick_interval=0)
        result = await sim.rescue_plan("btcusdt")
        assert result["plan"]["symbol"] == "BTCUSDT" and result["explanation"]["mock"] is True
        assert await sim.rescue_plan("NOPE") is None
        snap = sim.snapshot()
        assert snap["funding"]["BTCUSDT"]["source"] == "demo"
        assert set(snap["wallets"]) == {"binance", "hyperliquid"}
        assert snap["minutes_per_step"] == 12
        ov = snap["overview"]["positions"][0]
        assert ov["true_liquidation"]["margin_mode"] == "cross" and ov["funding"]["label"] == "at current rate"

    async def test_no_funding_keeps_path_exact(self):
        sim = MarketSimulation(None, tick_interval=0, funding=())
        await run(sim)
        btc = sim._position("BTCUSDT")
        spec = next(s for s in sim.specs if s.symbol == "BTCUSDT")
        assert btc.distance_to_liquidation_pct == pytest.approx(spec.end_distance_pct, abs=1e-6)
        assert not any(a["level"] == "funding" for a in sim.alerts)


# --------------------------------------------------------------------------- Telegram


@pytest.fixture
def handlers(ctx):
    from liqguard.telegram import BotHandlers

    return BotHandlers(ctx)


def buttons(message) -> list[str]:
    markup = message.answer.call_args.kwargs.get("reply_markup")
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


class TestTelegram:
    async def test_positions_show_true_liquidation_and_funding(self, ctx, handlers):
        await ctx.storage.save_user(User(telegram_id=ALICE, is_demo_mode=True))
        sim = handlers.simulation_for(await ctx.storage.get_user(ALICE))
        await sim.start()
        await sim.wait()
        message = fake_message(ALICE, "/positions")
        await handlers.cmd_positions(message)
        text = message.answer.call_args.args[0]
        assert "Liq: $" in text and "true, cross" in text and "other PnL" in text
        assert "Funding: +0.2500%/8h (demo rate)" in text
        assert "drift at current rate: 8h →" in text
        assert "rescue:sim:BTCUSDT" in buttons(message)

    async def test_risk_shows_riskiest_liquidation_and_rescue(self, ctx, handlers):
        await ctx.storage.save_user(User(telegram_id=ALICE, is_demo_mode=True))
        sim = handlers.simulation_for(await ctx.storage.get_user(ALICE))
        await sim.start()
        await sim.wait()
        message = fake_message(ALICE, "/risk")
        await handlers.cmd_risk(message)
        text = message.answer.call_args.args[0]
        assert "liquidation & funding" in text
        assert any(b.startswith("rescue:sim:") for b in buttons(message))

    async def test_rescue_callback_compares_options_with_buttons(self, ctx, handlers):
        await ctx.storage.save_user(User(telegram_id=ALICE, is_demo_mode=True))
        sim = handlers.simulation_for(await ctx.storage.get_user(ALICE))
        sim.step = 30
        sim.positions = sim._positions_at(sim.step)
        cb = fake_callback(ALICE, "rescue:sim:BTCUSDT")
        await handlers.callback_rescue(cb)
        text = cb.message.answer.call_args.args[0]
        assert "Rescue simulator" in text and "Add $" in text and "Reduce" in text and "Hedge" in text
        assert "24h funding" in text and "Muse (mock)" in text
        markup = cb.message.answer.call_args.kwargs["reply_markup"]
        assert [b.callback_data for b in markup.inline_keyboard[0]] == [
            "rescue:sim:BTCUSDT:add_margin", "rescue:sim:BTCUSDT:reduce", "rescue:sim:BTCUSDT:hedge"]

        detail = fake_callback(ALICE, "rescue:sim:BTCUSDT:reduce")
        await handlers.callback_rescue(detail)
        assert "Realized loss" in detail.message.answer.call_args.args[0]

    async def test_rescue_callback_is_scoped_to_the_caller(self, ctx, handlers):
        await ctx.storage.save_user(User(telegram_id=ALICE, is_demo_mode=True))
        await ctx.storage.save_user(User(telegram_id=BOB))
        handlers.simulation_for(await ctx.storage.get_user(ALICE))
        cb = fake_callback(BOB, "rescue:sim:BTCUSDT")
        await handlers.callback_rescue(cb)
        cb.message.answer.assert_not_called()
        cb.answer.assert_awaited_with("Position not found")

    async def test_sim_bridge_sends_prefunding_and_rescue_buttons(self, ctx):
        from liqguard.telegram.sim_bridge import TelegramSimulationBridge

        sim = MarketSimulation(MetaLLMClient(use_mock=True), owner_id=42, tick_interval=0)
        bot = AsyncMock()
        TelegramSimulationBridge(bot, sim)
        await sim.start()
        await sim.wait()
        texts = [c.args[1] for c in bot.send_message.call_args_list]
        assert any("PRE-FUNDING ALERT" in t for t in texts)
        assert any("Cheapest rescue" in t for t in texts)
        callbacks = [b.callback_data for c in bot.send_message.call_args_list if c.kwargs.get("reply_markup")
                     for row in c.kwargs["reply_markup"].inline_keyboard for b in row]
        assert "rescue:sim:BTCUSDT:hedge" in callbacks


class TestMonitor:
    async def test_prefunding_alert_once_per_settlement_with_rescue_button(self):
        user = User(telegram_id=ALICE)
        near = position(mode=ISOLATED, mark=47000, entry=50000, size=1, wallet=5000)
        info = FundingInfo(Venue.BINANCE, "BTCUSDT", 0.003, 8, NOW + timedelta(minutes=20))
        snapshot = BookSnapshot([near], "📊 Live Positions", RiskContext(funding={("binance", "BTCUSDT"): info}, now=NOW))

        async def users():
            return [user]

        async def snapshots(_):
            return snapshot

        notifier = AsyncMock()
        monitor = PositionMonitor(users, snapshots, notifier, AlertManager(), MetaLLMClient(use_mock=True))
        await monitor.check_all_users()
        await monitor.check_all_users()
        texts = [c.args[1] for c in notifier.notify.call_args_list]
        assert sum("PRE-FUNDING" in t for t in texts) == 1
        prefunding = next(c for c in notifier.notify.call_args_list if "PRE-FUNDING" in c.args[1])
        assert prefunding.args[0] == ALICE
        assert prefunding.kwargs["actions"] == [("🛟 Rescue options", "rescue:binance:BTCUSDT")]

    async def test_alerts_use_the_closer_computed_distance(self):
        user = User(telegram_id=ALICE, threshold=UserThreshold())
        # exchange says 25% away (safe); the computed isolated liquidation is ~3.9% away
        p = position(mode=ISOLATED, mark=47000, entry=50000, size=1, wallet=5000)
        p = Position(p.venue, p.symbol, p.side, p.size, p.entry_price, p.mark_price, 35250.0, p.margin, p.leverage,
                     p.unrealized_pnl, margin_mode=ISOLATED, isolated_wallet=5000)

        async def users():
            return [user]

        async def snapshots(_):
            return BookSnapshot([p], "📊 Live Positions", RiskContext(now=NOW))

        notifier = AsyncMock()
        await PositionMonitor(users, snapshots, notifier, AlertManager(), MetaLLMClient(use_mock=True)).check_all_users()
        assert "EMERGENCY" in notifier.notify.call_args_list[0].args[1]


# --------------------------------------------------------------------------- dashboard


class TestDashboard:
    def test_public_rescue_endpoint(self):
        sim = MarketSimulation(MetaLLMClient(use_mock=True), tick_interval=0)
        with TestClient(create_app(demo_simulation=sim)) as client:
            body = client.get("/api/demo/rescue", params={"symbol": "ETH"}).json()
            assert body["plan"]["venue"] == "hyperliquid"
            assert body["plan"]["options"][2]["protects_liquidation"] is False
            assert body["explanation"]["recommended_option"] == body["plan"]["recommended"]
            assert client.get("/api/demo/rescue", params={"symbol": "DOGE"}).status_code == 404
            assert client.get("/api/demo/rescue").status_code == 422

    def test_index_has_rescue_and_funding_panels(self):
        with TestClient(create_app(demo_simulation=MarketSimulation(None))) as client:
            html = client.get("/").text
            assert 'id="rescueCard"' in html and 'id="funding"' in html and 'id="cntFunding"' in html

    async def test_live_rescue_is_scoped_to_the_session_user(self, ctx):
        await ctx.storage.save_user(User(telegram_id=ALICE, is_demo_mode=True))
        await ctx.storage.save_user(User(telegram_id=BOB))
        with TestClient(create_app(ctx), base_url="https://testserver") as client:
            assert client.get("/api/me/rescue/live", params={"symbol": "BTCUSDT", "venue": "demo"}).status_code == 401
            assert client.get("/api/me/rescue", params={"symbol": "BTCUSDT"}).status_code == 401

            client.cookies.set(SESSION_COOKIE, ctx.sessions.issue(ALICE, 0))
            mine = client.get("/api/me/rescue/live", params={"symbol": "BTCUSDT", "venue": "demo"})
            assert mine.status_code == 200 and mine.json()["plan"]["symbol"] == "BTCUSDT"
            account = client.get("/api/me/account").json()
            assert account["positions"]["positions"][0]["true_liquidation"]["margin_mode"] == "isolated"

            client.cookies.set(SESSION_COOKIE, ctx.sessions.issue(BOB, 0))
            assert client.get("/api/me/rescue/live", params={"symbol": "BTCUSDT", "venue": "demo"}).status_code == 404
            sim_plan = client.get("/api/me/rescue", params={"symbol": "BTCUSDT"})
            assert sim_plan.status_code == 200
            assert ctx.simulations.get(BOB) is not None and ctx.simulations.get(ALICE) is None
