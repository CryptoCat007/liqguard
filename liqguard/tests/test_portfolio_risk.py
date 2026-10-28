"""Priority triage, position sizing, correlated portfolio stress test, concentration and platform vs correlated liq."""

import asyncio
import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from liqguard.ai import MetaLLMClient
from liqguard.alerts import (
    AlertManager,
    PositionMonitor,
    format_correlation_message,
    format_sizing_message,
    format_triage_message,
    priority_line,
)
from liqguard.core import BookSnapshot
from liqguard.models import (
    CROSS,
    ISOLATED,
    STOP_MARKET,
    MarginAccount,
    OpenOrder,
    Position,
    Side,
    User,
    UserThreshold,
    Venue,
)
from liqguard.risk import (
    HANDLE_FIRST,
    RiskContext,
    account_equity,
    account_surplus,
    concentration,
    correlation,
    default_brackets,
    factor,
    liq_comparison,
    liquidating_move,
    portfolio_insights,
    sigma_5m,
    size_check,
    stress_account,
    stress_band,
    time_to_liquidation_minutes,
    triage,
    true_liquidation,
)
from liqguard.simulation import SCENARIOS, MarketSimulation
from liqguard.simulation.demo_data import DEMO_WICKS
from liqguard.web import create_app
from liqguard.web.auth import SESSION_COOKIE

from .conftest import fake_callback, fake_message

ALICE, BOB = 1001, 2002
CSRF = {"X-LiqGuard-CSRF": "1"}
NOW = datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc)

FIVE_LONGS = (("BTCUSDT", 0.25, 64000.0), ("ETHUSDT", 5.0, 3150.0), ("SOLUSDT", 100.0, 148.0),
              ("DOGEUSDT", 90000.0, 0.165), ("AVAXUSDT", 500.0, 29.0))


def long_position(symbol: str, size: float, mark: float, entry: float = None, mode: str = CROSS,
                  leverage: float = 10.0, side: Side = Side.LONG, venue: Venue = Venue.BINANCE) -> Position:
    entry = mark if entry is None else entry
    margin = size * entry / leverage
    sign = 1 if side == Side.LONG else -1
    return Position(venue, symbol, side, size, entry, mark, 0.0, margin, leverage, sign * (mark - entry) * size,
                    margin_mode=mode, isolated_wallet=margin if mode == ISOLATED else None)


def book(positions: list[Position], wallet: float, venue: Venue = Venue.BINANCE) -> tuple[list[Position], MarginAccount]:
    """Positions with the platform-style liquidation price (others frozen) filled in, and their account."""
    schedules = {p.symbol: default_brackets(p.venue, p.symbol) for p in positions}
    account = MarginAccount(venue, wallet, positions, schedules)
    filled = [Position(**{**p.__dict__, "liquidation_price": true_liquidation(p, account).price or 0.0}) for p in positions]
    return filled, MarginAccount(venue, wallet, filled, schedules)


def five_longs(wallet: float = 9000.0) -> tuple[list[Position], MarginAccount]:
    return book([long_position(s, q, m) for s, q, m in FIVE_LONGS], wallet)


def sim_at(step: int, scenario: str = "cross_longs") -> MarketSimulation:
    sim = MarketSimulation(MetaLLMClient(use_mock=True), scenario=scenario, tick_interval=0, escalation_seconds_per_minute=0.01)
    sim.step = step
    sim.positions = sim._positions_at(step)
    return sim


# --------------------------------------------------------------------------- correlation


class TestCorrelation:
    def test_same_underlying_is_perfectly_correlated_across_venues(self):
        assert correlation("ETHUSDT", "ETH") == 1.0
        assert correlation("ETH-PERP", "ETHUSDT") == 1.0

    def test_majors_are_correlated_and_stocks_are_not(self):
        assert correlation("BTCUSDT", "ETHUSDT") == pytest.approx(0.85)
        assert correlation("SOLUSDT", "DOGEUSDT") >= 0.5
        assert correlation("BTCUSDT", "TSLAUSDT") < 0.3

    def test_unknown_coin_gets_crypto_defaults(self):
        f = factor("PEPEUSDT")
        assert f.beta > 1 and 0.5 < f.rho < 1


# --------------------------------------------------------------------------- stress test


class TestStress:
    def test_surplus_at_zero_move_is_equity_minus_maintenance(self):
        positions, account = five_longs()
        equity, maint = account_surplus(account, 0.0)
        assert equity == pytest.approx(9000.0 + sum(p.unrealized_pnl for p in positions))
        assert 0 < maint < 0.02 * sum(p.notional_value for p in positions)

    def test_liquidating_move_finds_the_root(self):
        assert liquidating_move(lambda m: 10 + m, -1, 95) == pytest.approx(10, abs=1e-6)
        assert liquidating_move(lambda m: 10 + m, 1, 95) is None
        assert liquidating_move(lambda m: -1.0, -1, 95) == 0.0

    def test_five_correlated_longs_die_together_long_before_any_platform_line(self):
        positions, account = five_longs()
        stress = stress_account(account)
        shocks = {s.market_move_pct: s.liquidated for s in stress.shocks}
        assert shocks[-5.0] is False and shocks[-10.0] is True and shocks[-20.0] is True
        assert shocks[5.0] is False and shocks[20.0] is False
        assert 7 < stress.liq_move_down_pct < 10
        # high-beta alts fall harder than the market, so the correlated model liquidates earlier than "all move equally"
        assert stress.liq_move_down_pct < stress.uniform_liq_move_down_pct
        assert stress.liq_move_up_pct is None
        assert stress.first_liquidation == pytest.approx(-stress.liq_move_down_pct)
        assert min(p.distance_to_liquidation_pct for p in positions) > 45

    def test_short_book_is_liquidated_by_a_rally(self):
        _, account = book([long_position("BTCUSDT", 1.0, 64000.0, side=Side.SHORT),
                           long_position("ETHUSDT", 20.0, 3150.0, side=Side.SHORT)], 15000.0)
        stress = stress_account(account)
        assert stress.liq_move_down_pct is None and stress.liq_move_up_pct is not None
        assert stress.first_liquidation > 0

    def test_isolated_only_account_has_no_cross_stress(self):
        _, account = book([long_position("BTCUSDT", 0.1, 64000.0, mode=ISOLATED)], 1000.0)
        assert stress_account(account) is None

    def test_bigger_wallet_survives_bigger_moves(self):
        small = stress_account(five_longs(9000.0)[1]).liq_move_down_pct
        big = stress_account(five_longs(30000.0)[1]).liq_move_down_pct
        assert big > small

    @pytest.mark.parametrize("move,band", [(None, None), (-20.0, None), (-9.0, "warning"), (4.0, "critical"), (-1.0, "emergency")])
    def test_stress_bands(self, move, band):
        assert stress_band(move) == band


class TestLiqComparison:
    def test_platform_line_vs_correlated_liq_gap_is_highlighted(self):
        positions, account = five_longs()
        stress = stress_account(account)
        btc = positions[0]
        c = liq_comparison(btc, account, btc.liquidation_price, stress)
        assert c.platform_price == pytest.approx(btc.liquidation_price)
        assert c.correlated_price > c.platform_price
        assert c.correlated_distance_pct == pytest.approx(stress.liq_move_down_pct * factor("BTCUSDT").beta, rel=1e-3)
        assert c.gap_pct > 30
        d = c.to_dict()
        assert d["gap_pct"] == round(c.gap_pct, 2) and d["correlated_market_move_pct"] < 0

    def test_correlated_price_is_never_further_than_the_frozen_one(self):
        positions, account = book([long_position("BTCUSDT", 1.0, 64000.0), long_position("TSLAUSDT", 10.0, 250.0)], 8000.0)
        stress = stress_account(account)
        for p in positions:
            c = liq_comparison(p, account, p.liquidation_price, stress)
            assert c.correlated_distance_pct <= c.platform_distance_pct + 1e-9

    def test_isolated_position_keeps_its_own_liq(self):
        p = long_position("SOLUSDT", 10.0, 150.0, mode=ISOLATED)
        true = true_liquidation(p, None).price
        c = liq_comparison(p, None, true, None)
        assert c.correlated_price == pytest.approx(true)
        assert c.gap_pct == pytest.approx(0.0, abs=1e-9)
        assert c.correlated_market_move_pct == pytest.approx(-c.correlated_distance_pct / factor("SOLUSDT").beta)


class TestConcentration:
    def test_all_long_correlated_book_is_flagged(self):
        positions, _ = five_longs()
        conc = concentration(positions)
        assert conc.flagged and conc.dominant_side == "long" and conc.dominant_share_pct == pytest.approx(100)
        assert conc.dominant_count == 5 and conc.avg_correlation >= 0.6
        assert conc.effective_bets < 2
        assert "move together" in conc.message() and "independent bets" in conc.message()

    def test_hedged_book_is_not_flagged(self):
        positions = [long_position("BTCUSDT", 1.0, 64000.0), long_position("ETHUSDT", 20.0, 3150.0, side=Side.SHORT)]
        conc = concentration(positions)
        assert not conc.flagged and conc.message() is None
        assert conc.effective_bets > 2

    def test_single_position_and_uncorrelated_pairs_are_not_flagged(self):
        assert not concentration([long_position("BTCUSDT", 1.0, 64000.0)]).flagged
        assert not concentration([long_position("TSLAUSDT", 100.0, 250.0), long_position("NVDAUSDT", 100.0, 120.0)]).flagged
        assert concentration([]).dominant_side is None


# --------------------------------------------------------------------------- sizing


class TestSizing:
    def test_equity_counts_cross_pnl_and_isolated_margin(self):
        cross = long_position("BTCUSDT", 0.1, 60000.0, entry=64000.0)
        iso = long_position("SOLUSDT", 10.0, 160.0, entry=150.0, mode=ISOLATED)
        account = MarginAccount(Venue.BINANCE, 5000.0, [cross, iso], {})
        assert account_equity(account) == pytest.approx(5000 - 400 + iso.margin + 100)

    def test_stop_that_fires_first_is_the_exit(self):
        from liqguard.risk import check_stop_loss

        positions, account = book([long_position("BTCUSDT", 0.5, 64000.0, leverage=5)], 20000.0)
        p = positions[0]
        stop = check_stop_loss(p, [OpenOrder(Venue.BINANCE, "BTCUSDT", Side.SHORT, STOP_MARKET, trigger_price=62000.0,
                                             close_position=True)], p.liquidation_price, account, None)
        check = size_check(p, account, UserThreshold(risk_per_trade_pct=6, max_margin_pct=50), p.liquidation_price, stop)
        assert check.exit_kind == "stop" and check.exit_price == 62000.0
        assert check.loss_at_exit_usd == pytest.approx(1000.0)
        assert check.risk_pct == pytest.approx(1000 / 20000 * 100)
        assert check.status == "ok" and check.reduce_pct is None and check.reasons == []
        tight = size_check(p, account, UserThreshold(risk_per_trade_pct=2, max_margin_pct=50), p.liquidation_price, stop)
        assert tight.status == "oversized" and tight.suggested_size == pytest.approx(0.02 * 20000 / 2000)

    def test_oversized_position_gets_a_size_that_meets_both_limits(self):
        positions, account = five_longs()
        t = UserThreshold(risk_per_trade_pct=2.0, max_margin_pct=25.0)
        for p in positions:
            check = size_check(p, account, t, p.liquidation_price)
            assert check.status == "oversized" and check.exit_kind == "liquidation price"
            q = check.suggested_size
            equity = check.equity
            assert q * abs(p.mark_price - p.liquidation_price) <= 0.02 * equity * 1.0001
            assert q * p.margin / p.size <= 0.25 * equity * 1.0001
            assert check.reasons and 0 < check.reduce_pct < 100

    def test_margin_limit_alone_can_bind(self):
        positions, account = book([long_position("BTCUSDT", 1.0, 64000.0, leverage=2)], 40000.0)
        p = positions[0]
        stop_like = p.mark_price * 0.999
        check = size_check(p, account, UserThreshold(risk_per_trade_pct=20, max_margin_pct=10), stop_like)
        assert check.margin_pct > 10 and any("margin" in r for r in check.reasons)
        assert check.suggested_size * p.margin / p.size == pytest.approx(0.10 * check.equity, rel=1e-6)

    def test_no_account_means_no_check(self):
        assert size_check(long_position("BTCUSDT", 1.0, 64000.0), None, UserThreshold(), 50000.0) is None

    def test_threshold_round_trips_the_new_settings(self):
        t = UserThreshold(risk_per_trade_pct=1.5, max_margin_pct=40)
        back = UserThreshold.from_dict(t.to_dict())
        assert back.risk_per_trade_pct == 1.5 and back.max_margin_pct == 40
        legacy = {k: v for k, v in t.to_dict().items() if k not in ("risk_per_trade_pct", "max_margin_pct")}
        assert UserThreshold.from_dict(legacy).risk_per_trade_pct == 2.0


# --------------------------------------------------------------------------- triage


def _entry(symbol: str, distance: float, size: float = 1.0, mark: float = 100.0, profile=None, tiers=None):
    p = long_position(symbol, size, mark)
    liq = mark * (1 - distance / 100)
    return p, distance, liq, tiers or UserThreshold(), profile


class TestTriage:
    def test_time_to_liquidation_scales_with_distance_over_volatility(self):
        assert time_to_liquidation_minutes(2.0, 0.5) == pytest.approx(5 * 16)
        assert time_to_liquidation_minutes(math.inf, 0.5) == math.inf
        assert time_to_liquidation_minutes(0.0, 0.5) == 0.0

    def test_sigma_comes_from_wicks_or_defaults(self):
        sol = long_position("SOLUSDT", 1.0, 150.0)
        sigma, source = sigma_5m(sol, DEMO_WICKS["SOLUSDT"])
        assert source == "wicks" and sigma == pytest.approx(0.85 / 1.645)
        assert sigma_5m(sol, None)[1] == "default"
        assert sigma_5m(long_position("BTCUSDT", 1.0, 1.0), None)[0] < sigma_5m(long_position("PEPEUSDT", 1.0, 1.0), None)[0]
        assert sigma_5m(long_position("TSLAUSDT", 1.0, 1.0), None)[0] < sigma_5m(long_position("BTCUSDT", 1.0, 1.0), None)[0]

    def test_volatile_coin_further_away_can_outrank_a_calm_closer_one(self):
        items = triage([_entry("BTCUSDT", 4.0), _entry("PEPEUSDT", 5.0)])
        btc, pepe = (next(i for i in items if i.symbol == s) for s in ("BTCUSDT", "PEPEUSDT"))
        assert pepe.minutes_to_liq < btc.minutes_to_liq
        assert items[0].symbol == "PEPEUSDT" and items[0].label == HANDLE_FIRST
        assert [i.rank for i in items] == [1, 2] and all(i.of == 2 for i in items)

    def test_at_risk_positions_rank_above_safe_ones(self):
        items = triage([_entry("DOGEUSDT", 30.0, size=1e6), _entry("BTCUSDT", 15.0, size=0.001)])
        assert items[0].symbol == "BTCUSDT" and items[0].at_risk and items[0].label == HANDLE_FIRST
        assert not items[1].at_risk and items[1].label is None

    def test_size_breaks_ties(self):
        items = triage([_entry("ETHUSDT", 8.0, size=1.0), _entry("ETH-PERP", 8.0, size=5.0)])
        assert items[0].symbol == "ETH-PERP"

    def test_no_label_when_nothing_is_at_risk(self):
        items = triage([_entry("BTCUSDT", 40.0), _entry("ETHUSDT", 50.0)])
        assert all(i.label is None for i in items)
        assert triage([]) == []

    def test_to_dict_and_reason(self):
        item = triage([_entry("SOLUSDT", 6.0), _entry("BTCUSDT", 9.0)])[0]
        d = item.to_dict()
        assert d["label"] == HANDLE_FIRST and d["rank"] == 1 and d["minutes_to_liq"] > 0
        assert "typical 5m moves" in d["reason"] and "notional" in d["reason"]


# --------------------------------------------------------------------------- insights (combined)


class TestInsights:
    def test_five_long_scenario_insights_end_to_end(self):
        sim = sim_at(37)
        ins = sim.insights()
        d = ins.to_dict()
        assert len(d["triage"]) == 5 and d["at_risk_count"] == 5
        assert d["top"]["label"] == HANDLE_FIRST and d["triage"][0]["symbol"] == d["top"]["symbol"]
        assert d["worst_account"]["venue"] == "binance" and d["worst_account"]["first_liquidation_move_pct"] < 0
        assert d["concentration"]["flagged"]
        assert all(c["gap_pct"] > 1 for c in d["comparisons"])
        assert set(d["oversized"]) == {s for s, _, _ in FIVE_LONGS}
        assert all(s["exit_kind"] == "correlated liquidation" for s in d["sizing"])
        btc = sim._position("BTCUSDT")
        assert ins.priority(btc).symbol == "BTCUSDT" and ins.comparison(btc).symbol == "BTCUSDT"

    def test_run_start_shows_platform_lines_far_away_but_account_near_liquidation(self):
        d = sim_at(0).insights().to_dict()
        assert d["at_risk_count"] == 0 and d["top"] is None
        assert min(c["platform_distance_pct"] for c in d["comparisons"]) > 45
        assert abs(d["worst_account"]["first_liquidation_move_pct"]) < 10

    def test_mixed_book_keeps_isolated_tsla_out_of_the_cross_stress(self):
        d = sim_at(0, "mixed").insights().to_dict()
        binance = next(s for s in d["stress"] if s["venue"] == "binance")
        assert "TSLAUSDT" not in binance["symbols"] and set(binance["symbols"]) == {"BTCUSDT", "SOLUSDT"}
        assert not d["concentration"]["flagged"]

    def test_works_without_account_data(self):
        p = long_position("BTCUSDT", 1.0, 64000.0)
        p = Position(**{**p.__dict__, "liquidation_price": 60000.0})
        ins = portfolio_insights([p], RiskContext(now=NOW), UserThreshold())
        assert ins.stress == [] and ins.sizing == [] and len(ins.triage) == 1
        assert ins.to_dict()["worst_account"] is None


# --------------------------------------------------------------------------- alert texts


class TestMessages:
    def test_triage_message_lists_positions_in_order(self):
        items = [t.to_dict() for t in sim_at(37).insights().at_risk]
        text = format_triage_message(items, "emergency", demo=True)
        assert "5 POSITIONS AT RISK" in text and "demo" in text
        lines = [line for line in text.splitlines() if line[:2] in {f"{n}." for n in range(1, 6)}]
        assert len(lines) == 5 and HANDLE_FIRST in lines[0] and items[0]["symbol"] in lines[0]

    def test_priority_line(self):
        top = {"symbol": "SOLUSDT", "label": HANDLE_FIRST, "rank": 1, "of": 3}
        assert HANDLE_FIRST in priority_line(top, top)
        assert "handle **SOLUSDT** first" in priority_line({"rank": 2, "of": 3, "label": None}, top)
        assert priority_line({"rank": 1, "of": 1, "label": HANDLE_FIRST}, top) is None

    def test_sizing_and_correlation_messages(self):
        d = sim_at(0).insights().to_dict()
        sizing = format_sizing_message(d["sizing"])
        assert "POSITION SIZE TOO LARGE" in sizing and "suggested" in sizing and "/settings risk" in sizing
        corr = format_correlation_message(d["stress"][0], d["concentration"], d["comparisons"], "warning")
        assert "PORTFOLIO STRESS TEST" in corr and "LIQUIDATED" in corr
        assert "platform liq" in corr and "LiqGuard correlated liq" in corr and "pts closer" in corr
        assert "$0.1" in corr  # sub-dollar prices keep their digits


# --------------------------------------------------------------------------- live monitor


class TestMonitor:
    async def _monitor(self, snapshot, user):
        async def users():
            return [user]

        async def snapshots(_):
            return snapshot

        notifier = AsyncMock()
        return PositionMonitor(users, snapshots, notifier, AlertManager(), MetaLLMClient(use_mock=True)), notifier

    async def test_several_positions_due_at_once_get_one_consolidated_alert(self):
        sim = sim_at(37)
        user = User(telegram_id=ALICE, threshold=sim.threshold)
        monitor, notifier = await self._monitor(BookSnapshot(sim.positions, "📊 Live", sim.risk_context()), user)
        await monitor.check_user(user)
        texts = [c.args[1] for c in notifier.notify.call_args_list]
        assert all(c.args[0] == ALICE for c in notifier.notify.call_args_list)
        assert sum("HANDLE IN THIS ORDER" in t for t in texts) == 1
        assert not any(" ALERT**" in t and "POSITIONS AT RISK" not in t and "PRE-FUNDING" not in t for t in texts)
        triage_call = next(c for c in notifier.notify.call_args_list if "HANDLE IN THIS ORDER" in c.args[1])
        top = sim.insights().top.symbol
        assert ("🛟 Rescue options", f"rescue:binance:{top}") in triage_call.kwargs["actions"]

    async def test_sizing_and_stress_warnings_fire_once_until_they_get_worse(self):
        sim = sim_at(0)
        user = User(telegram_id=ALICE, threshold=sim.threshold)
        snapshot = BookSnapshot(sim.positions, "📊 Live", sim.risk_context())
        monitor, notifier = await self._monitor(snapshot, user)
        await monitor.check_user(user)
        await monitor.check_user(user)
        texts = [c.args[1] for c in notifier.notify.call_args_list]
        assert sum("POSITION SIZE TOO LARGE" in t for t in texts) == 1
        assert sum("PORTFOLIO STRESS TEST" in t for t in texts) == 1

        worse = sim_at(30)
        snapshot.positions, snapshot.context = worse.positions, worse.risk_context()
        await monitor.check_user(user)
        texts = [c.args[1] for c in notifier.notify.call_args_list]
        assert sum("PORTFOLIO STRESS TEST" in t for t in texts) == 2
        assert sum("POSITION SIZE TOO LARGE" in t for t in texts) == 1

    async def test_single_tier_alert_carries_priority_and_correlated_liq(self):
        sim = sim_at(23)
        user = User(telegram_id=ALICE, threshold=sim.threshold)
        monitor, notifier = await self._monitor(BookSnapshot(sim.positions, "📊 Live", sim.risk_context()), user)
        await monitor.check_user(user)
        tier = [c.args[1] for c in notifier.notify.call_args_list if "WARNING ALERT" in c.args[1]]
        assert len(tier) == 1
        assert "LiqGuard correlated liq" in tier[0]

    async def test_users_never_see_each_others_portfolio_warnings(self):
        sim = sim_at(0)
        alice, bob = User(telegram_id=ALICE, threshold=sim.threshold), User(telegram_id=BOB)
        books = {ALICE: BookSnapshot(sim.positions, "📊 Live", sim.risk_context()), BOB: BookSnapshot([], "📊 Live", None)}

        async def users():
            return [alice, bob]

        async def snapshots(u):
            return books[u.telegram_id]

        notifier = AsyncMock()
        await PositionMonitor(users, snapshots, notifier, AlertManager(), MetaLLMClient(use_mock=True)).check_all_users()
        assert notifier.notify.call_args_list and {c.args[0] for c in notifier.notify.call_args_list} == {ALICE}


# --------------------------------------------------------------------------- simulation


class TestSimulationScenario:
    async def test_cross_longs_run_emits_triage_sizing_and_correlation(self):
        sim = MarketSimulation(MetaLLMClient(use_mock=True), scenario="cross_longs", tick_interval=0, escalation_seconds_per_minute=0.01)
        events = []

        async def collect(e):
            events.append(e)

        sim.subscribe("test", collect)
        await sim.start()
        await sim.wait()
        await sim.stop()
        levels = [a["level"] for a in sim.alerts]
        assert {"triage", "sizing", "correlation", "warning", "critical", "emergency", "stop"} <= set(levels)
        assert levels.count("sizing") == 1
        bands = [a["severity"] for a in sim.alerts if a["level"] == "correlation"]
        assert bands[0] == "warning" and "emergency" in bands and len(bands) == len(set(bands))

        triages = [a for a in sim.alerts if a["level"] == "triage"]
        for t in triages:
            assert [i["rank"] for i in t["items"]] == list(range(1, len(t["items"]) + 1))
            assert t["items"][0]["label"] == HANDLE_FIRST and t["symbol"] == t["items"][0]["symbol"]
            members = [a for a in sim.alerts if a.get("consolidated") == t["id"]]
            assert len({a["symbol"] for a in members}) >= 2
            assert sum(1 for a in members if a.get("ack_id")) <= 1
        consolidated_ids = {a["id"] for a in sim.alerts if a.get("consolidated")}
        advised = {c["alert_id"] for c in sim.advice}
        assert len(consolidated_ids & advised) <= len(triages)
        tier = [a for a in sim.alerts if a["level"] in ("warning", "critical", "emergency")]
        assert all(a["priority"]["of"] == 5 for a in tier)
        ticks = [e["data"] for e in events if e["type"] == "tick"]
        assert ticks and all(t["scenario"] == "cross_longs" and "insights" in t for t in ticks)

    async def test_switching_scenarios(self):
        sim = MarketSimulation(MetaLLMClient(use_mock=True), tick_interval=0.05)
        assert sim.scenario == "mixed" and set(sim.history) == {"BTCUSDT", "ETH", "SOLUSDT", "TSLAUSDT"}
        assert sim.set_scenario("nope") is False
        assert await sim.switch_scenario("cross_longs") is True
        snap = sim.snapshot()
        assert snap["scenario"] == "cross_longs" and set(snap["history"]) == {s for s, _, _ in FIVE_LONGS}
        assert {s["key"] for s in snap["scenarios"]} == set(SCENARIOS)
        assert snap["wallets"] == {"binance": 9000.0}
        assert snap["insights"]["concentration"]["flagged"]
        sim.configure(UserThreshold(), "en")
        assert sim.scenario == "cross_longs"
        await sim.start()
        assert await sim.switch_scenario("mixed") is False
        await sim.stop()
        assert await sim.switch_scenario("mixed") is True and sim.scenario == "mixed"

    async def test_bridge_sends_consolidated_sizing_and_stress_messages(self):
        from liqguard.telegram.sim_bridge import TelegramSimulationBridge

        sim = MarketSimulation(MetaLLMClient(use_mock=True), owner_id=ALICE, scenario="cross_longs", tick_interval=0,
                               escalation_seconds_per_minute=0.01)
        bot = AsyncMock()
        TelegramSimulationBridge(bot, sim)
        await sim.start()
        await sim.wait()
        await sim.stop()
        calls = bot.send_message.call_args_list
        assert {c.args[0] for c in calls} == {ALICE}
        texts = [c.args[1] for c in calls]
        assert any("5 longs, one wallet" in t for t in texts)
        assert any("HANDLE IN THIS ORDER" in t for t in texts)
        assert sum("POSITION SIZE TOO LARGE" in t for t in texts) == 1
        assert any("PORTFOLIO STRESS TEST" in t and "pts closer" in t for t in texts)
        advice = [t for t in texts if " ALERT** · demo" in t]
        assert advice and any(HANDLE_FIRST in t for t in advice)
        triage_kb = next(c.kwargs["reply_markup"] for c in calls if "HANDLE IN THIS ORDER" in c.args[1])
        assert any(b.callback_data.startswith("rescue:sim:") for row in triage_kb.inline_keyboard for b in row)


# --------------------------------------------------------------------------- telegram


@pytest.fixture
def handlers(ctx):
    from liqguard.telegram import BotHandlers

    return BotHandlers(ctx)


class TestTelegram:
    async def test_demo_menu_offers_the_five_long_scenario_and_starts_it(self, ctx, handlers):
        message = fake_message(ALICE, "/demo")
        await handlers.cmd_demo(message)
        kb = message.answer.call_args.kwargs["reply_markup"]
        assert "demo:cross_longs_sim" in [b.callback_data for row in kb.inline_keyboard for b in row]

        callback = fake_callback(ALICE, "demo:cross_longs_sim")
        await handlers.callback_demo_scenario(callback)
        sim = ctx.simulations.get(ALICE)
        assert sim.scenario == "cross_longs" and "5 longs, one wallet" in callback.message.edit_text.call_args.args[0]
        await sim.stop()
        assert ctx.simulations.get(BOB) is None

    async def test_risk_shows_triage_stress_comparison_and_sizing(self, ctx, handlers):
        await ctx.storage.save_user(User(telegram_id=ALICE, is_demo_mode=True))
        sim = handlers.simulation_for(await ctx.storage.get_user(ALICE))
        await sim.switch_scenario("cross_longs")
        sim.step = 37
        sim.positions = sim._positions_at(37)
        sim.state = "stopped"
        message = fake_message(ALICE, "/risk")
        await handlers.cmd_risk(message)
        text = message.answer.call_args.args[0]
        assert "Priority triage" in text and HANDLE_FIRST in text
        assert "Stress test" in text and "LIQUIDATED" in text
        assert "LiqGuard correlated liq" in text and "pts closer" in text
        assert len(text) <= 4096
        kb = message.answer.call_args.kwargs["reply_markup"]
        assert any(b.callback_data.startswith("rescue:sim:") for row in kb.inline_keyboard for b in row)

    async def test_positions_show_priority_and_sizing_lines(self, ctx, handlers):
        await ctx.storage.save_user(User(telegram_id=ALICE, is_demo_mode=True))
        sim = handlers.simulation_for(await ctx.storage.get_user(ALICE))
        await sim.switch_scenario("cross_longs")
        sim.step = 37
        sim.positions = sim._positions_at(37)
        sim.state = "stopped"
        message = fake_message(ALICE, "/positions")
        await handlers.cmd_positions(message)
        text = message.answer.call_args.args[0]
        assert "Correlated liq" in text or "Oversized" in text or "Handle this first" in text

    async def test_settings_risk_and_maxmargin(self, ctx, handlers):
        message = fake_message(ALICE, "/settings")
        await handlers.cmd_settings(message, command=type("C", (), {"args": "risk 1.5"})())
        await handlers.cmd_settings(message, command=type("C", (), {"args": "maxmargin 40"})())
        t = (await ctx.storage.get_user(ALICE)).threshold
        assert t.risk_per_trade_pct == 1.5 and t.max_margin_pct == 40
        assert "**1.5%**" in message.answer.call_args.args[0]
        await handlers.callback_settings(fake_callback(ALICE, "settings:risk:0.5"))
        assert (await ctx.storage.get_user(ALICE)).threshold.risk_per_trade_pct == 0.5
        assert (await ctx.storage.get_user(BOB)) is None or (await ctx.storage.get_user(BOB)).threshold.risk_per_trade_pct == 2.0

    @pytest.mark.parametrize("key,value", [("risk", "0"), ("risk", "50"), ("risk", "x"), ("maxmargin", "0"), ("maxmargin", "500")])
    def test_settings_reject_out_of_range(self, handlers, key, value):
        assert handlers.apply_setting(UserThreshold(), key, value) is None


# --------------------------------------------------------------------------- dashboard


@pytest.fixture
def sim():
    return MarketSimulation(MetaLLMClient(use_mock=True), tick_interval=0, escalation_seconds_per_minute=0.01)


@pytest.fixture
def client(sim):
    with TestClient(create_app(demo_simulation=sim)) as c:
        yield c
        c.portal.call(sim.stop)


class TestDashboard:
    def test_insights_endpoint_and_snapshot(self, client):
        ins = client.get("/api/demo/insights").json()
        assert {"triage", "stress", "comparisons", "concentration", "sizing", "top", "worst_account"} <= set(ins)
        snap = client.get("/api/demo/snapshot").json()
        assert snap["scenario"] == "mixed" and {s["key"] for s in snap["scenarios"]} == {"mixed", "cross_longs"}
        assert snap["risk_settings"] == {"risk_per_trade_pct": 2.0, "max_margin_pct": 25.0}

    def test_scenario_switch_requires_csrf_and_a_known_key(self, client, sim):
        assert client.post("/api/demo/scenario", json={"scenario": "cross_longs"}).status_code == 403
        assert client.post("/api/demo/scenario", json={"scenario": "nope"}, headers=CSRF).status_code == 404
        assert client.post("/api/demo/scenario", json={"scenario": "../x"}, headers=CSRF).status_code == 422
        r = client.post("/api/demo/scenario", json={"scenario": "cross_longs"}, headers=CSRF)
        assert r.status_code == 200 and r.json()["scenario"] == "cross_longs"
        ins = client.get("/api/demo/insights").json()
        assert ins["concentration"]["flagged"] and len(ins["triage"]) == 5
        assert any(e["event"] == "scenario_switched" for e in client.get("/api/demo/audit").json())

    def test_cannot_switch_while_running(self, client, sim):
        sim.tick_interval = 0.05
        assert client.post("/api/demo/simulate", headers=CSRF).status_code == 200
        assert client.post("/api/demo/scenario", json={"scenario": "cross_longs"}, headers=CSRF).status_code == 409
        client.portal.call(sim.stop)

    def test_full_cross_longs_run_through_the_dashboard(self, client, sim):
        client.post("/api/demo/scenario", json={"scenario": "cross_longs"}, headers=CSRF)
        assert client.post("/api/demo/simulate", headers=CSRF).json()["status"] == "started"
        client.portal.call(sim.wait)
        levels = {a["level"] for a in client.get("/api/demo/alerts").json()}
        assert {"triage", "sizing", "correlation"} <= levels

    def test_page_has_the_new_cards(self, client):
        html = client.get("/").text
        for marker in ("scenarioSelect", "Priority triage", "Portfolio stress test", "Platform liq vs real liq",
                       "Position sizing", "concBanner"):
            assert marker in html


@pytest.fixture
def dashboard(ctx):
    with TestClient(create_app(ctx), base_url="https://testserver") as c:
        yield c


class TestPrivateDashboard:
    async def test_scenario_switch_is_per_user(self, ctx, dashboard):
        for uid in (ALICE, BOB):
            await ctx.storage.save_user(User(telegram_id=uid))
        dashboard.cookies.set(SESSION_COOKIE, ctx.sessions.issue(ALICE, 0))
        assert dashboard.post("/api/me/scenario", json={"scenario": "cross_longs"}, headers=CSRF).status_code == 200
        assert dashboard.get("/api/me/snapshot").json()["scenario"] == "cross_longs"
        dashboard.cookies.set(SESSION_COOKIE, ctx.sessions.issue(BOB, 0))
        assert dashboard.get("/api/me/snapshot").json()["scenario"] == "mixed"
        assert ctx.simulations.public.scenario == "mixed"

    def test_scenario_requires_a_session(self, dashboard):
        assert dashboard.post("/api/me/scenario", json={"scenario": "cross_longs"}, headers=CSRF).status_code == 401
        assert dashboard.get("/api/me/insights").status_code == 401

    async def test_account_includes_insights_for_the_demo_book(self, ctx, dashboard):
        await ctx.storage.save_user(User(telegram_id=ALICE, is_demo_mode=True))
        dashboard.cookies.set(SESSION_COOKIE, ctx.sessions.issue(ALICE, 0))
        body = dashboard.get("/api/me/account").json()
        assert body["insights"] is not None and len(body["insights"]["triage"]) == len(body["positions"]["positions"])

    async def test_settings_change_reaches_the_private_simulation_sizing(self, ctx, dashboard):
        await ctx.storage.save_user(User(telegram_id=ALICE, threshold=UserThreshold(risk_per_trade_pct=0.5)))
        dashboard.cookies.set(SESSION_COOKIE, ctx.sessions.issue(ALICE, 0))
        assert dashboard.post("/api/me/scenario", json={"scenario": "cross_longs"}, headers=CSRF).status_code == 200
        snap = dashboard.get("/api/me/snapshot").json()
        assert snap["risk_settings"]["risk_per_trade_pct"] == 0.5
        assert all(s["max_risk_pct"] == 0.5 for s in snap["insights"]["sizing"])
        await asyncio.sleep(0)
