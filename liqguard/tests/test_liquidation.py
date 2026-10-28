"""Liquidation math against hand-derived examples, the exchanges' formulas, and its own invariants."""

import random
from datetime import datetime, timezone

import pytest

from liqguard.models import (
    CROSS,
    ISOLATED,
    FundingInfo,
    MaintenanceBracket,
    MarginAccount,
    Position,
    Side,
    Venue,
)
from liqguard.risk import (
    RiskContext,
    build_brackets,
    maintenance_margin,
    solve_liquidation_price,
    true_liquidation,
)
from liqguard.risk.liquidation import (
    BINANCE_FALLBACK_BRACKETS,
    distance_pct,
    hyperliquid_brackets,
    solve_by_bisection,
    wallet_for_target,
)

FLAT_04 = (MaintenanceBracket(0, float("inf"), 0.004, 0),)
BTC_TIERS = BINANCE_FALLBACK_BRACKETS["BTCUSDT"]


def pos(symbol="BTCUSDT", side=Side.LONG, size=1.0, entry=50000.0, mark=50000.0, margin=5000.0,
        venue=Venue.BINANCE, mode=ISOLATED, liq=0.0, wallet=None) -> Position:
    sign = 1 if side == Side.LONG else -1
    return Position(venue, symbol, side, size, entry, mark, liq, margin, 10, sign * size * (mark - entry),
                    margin_mode=mode, isolated_wallet=wallet)


class TestKnownExamples:
    def test_isolated_long(self):
        # LP = (WB - Q*E) / (Q*MMR - Q) = (5000 - 50000) / (0.004 - 1) = 45000 / 0.996
        assert solve_liquidation_price(Side.LONG, 1, 50000, 5000, FLAT_04) == pytest.approx(45180.7229, abs=1e-4)

    def test_isolated_short(self):
        # LP = (WB + Q*E) / (Q*MMR + Q) = 55000 / 1.004
        assert solve_liquidation_price(Side.SHORT, 1, 50000, 5000, FLAT_04) == pytest.approx(54780.8765, abs=1e-4)

    def test_binance_tier_with_cum(self):
        # 10 BTC long, 50k entry, 60k wallet: Q·LP ≈ 444k lands in the 250k–1M tier (1%, cum 1300).
        lp = solve_liquidation_price(Side.LONG, 10, 50000, 60000, BTC_TIERS)
        tier = next(b for b in BTC_TIERS if b.contains(10 * lp))
        assert tier.mmr == 0.01 and tier.cum == pytest.approx(1300)
        # LP = (WB + cum - Q*E) / (Q*MMR - Q)
        assert lp == pytest.approx((60000 + 1300 - 500000) / (0.1 - 10))

    def test_fully_collateralised_long_has_no_liquidation(self):
        assert solve_liquidation_price(Side.LONG, 1, 100, 200, FLAT_04) is None
        result = true_liquidation(pos(entry=100, mark=100, margin=200))
        assert result.price is None and result.distance_pct == float("inf")

    def test_true_liquidation_isolated_uses_isolated_wallet(self):
        p = pos(mark=48000, margin=9999, wallet=5000)
        r = true_liquidation(p)
        assert r.price == pytest.approx(45000 / 0.996)
        assert r.distance_pct == pytest.approx((48000 - 45000 / 0.996) / 48000 * 100)
        assert r.margin_mode == ISOLATED


class TestTiers:
    def test_cum_makes_maintenance_continuous(self):
        for prev, nxt in zip(BTC_TIERS, BTC_TIERS[1:]):
            edge = nxt.notional_floor
            assert maintenance_margin(edge - 1e-6, BTC_TIERS) == pytest.approx(maintenance_margin(edge + 1e-6, BTC_TIERS), abs=1e-4)

    def test_build_brackets_derives_cum(self):
        tiers = build_brackets([(0, 0.004), (50000, 0.005), (250000, 0.01)])
        assert [t.cum for t in tiers] == pytest.approx([0, 50, 1300])
        assert tiers[-1].notional_cap == float("inf")

    @pytest.mark.parametrize("seed", range(40))
    def test_equity_equals_maintenance_at_liquidation(self, seed):
        rng = random.Random(seed)
        side = rng.choice([Side.LONG, Side.SHORT])
        q, e = rng.uniform(0.01, 60), rng.uniform(20000, 90000)
        wallet = q * e * rng.uniform(0.02, 0.5)
        other_upnl, other_mm = rng.uniform(-2000, 2000), rng.uniform(0, 500)
        lp = solve_liquidation_price(side, q, e, wallet, BTC_TIERS, other_upnl, other_mm)
        if lp is None:
            return
        s = 1 if side == Side.LONG else -1
        equity = wallet + other_upnl + s * q * (lp - e)
        assert equity == pytest.approx(other_mm + maintenance_margin(q * lp, BTC_TIERS), rel=1e-9, abs=1e-6)

    @pytest.mark.parametrize("seed", range(20))
    def test_closed_form_matches_bisection(self, seed):
        rng = random.Random(100 + seed)
        side = rng.choice([Side.LONG, Side.SHORT])
        q, e = rng.uniform(0.5, 40), rng.uniform(30000, 70000)
        wallet = q * e * rng.uniform(0.05, 0.4)
        s = 1 if side == Side.LONG else -1
        lp = solve_liquidation_price(side, q, e, wallet, BTC_TIERS)

        def surplus(p):
            return wallet + s * q * (p - e) - maintenance_margin(q * p, BTC_TIERS)

        assert solve_by_bisection(surplus, e, adverse_up=side == Side.SHORT) == pytest.approx(lp, rel=1e-7)


class TestCrossMargin:
    def account(self, btc: Position, others: list[Position], wallet=16000.0) -> MarginAccount:
        return MarginAccount(Venue.BINANCE, wallet, [btc, *others], {"BTCUSDT": FLAT_04, "SOLUSDT": FLAT_04})

    def test_other_positions_loss_and_maintenance_move_liquidation(self):
        btc = pos(size=0.8, entry=64200, mark=60000, mode=CROSS)
        sol = pos("SOLUSDT", size=220, entry=148, mark=140, mode=CROSS)
        r = true_liquidation(btc, self.account(btc, [sol]))
        # LP = (WB + other_upnl - other_mm - Q*E) / (Q*MMR - Q)
        other_upnl, other_mm = 220 * (140 - 148), 220 * 140 * 0.004
        expected = (16000 + other_upnl - other_mm - 0.8 * 64200) / (0.8 * 0.004 - 0.8)
        assert r.price == pytest.approx(expected)
        assert r.other_upnl == pytest.approx(other_upnl) and r.other_maintenance == pytest.approx(other_mm)
        alone = true_liquidation(btc, self.account(btc, []))
        assert r.price > alone.price  # SOL's loss pulls BTC's liquidation up

    def test_isolated_neighbours_do_not_count(self):
        btc = pos(size=0.8, entry=64200, mark=60000, mode=CROSS)
        sol = pos("SOLUSDT", size=220, entry=148, mark=100, mode=ISOLATED)
        assert true_liquidation(btc, self.account(btc, [sol])).price == pytest.approx(
            true_liquidation(btc, self.account(btc, [])).price
        )

    def test_hedged_cross_account_offsets(self):
        long_ = pos(size=1, entry=50000, mark=50000, mode=CROSS)
        short = pos("SOLUSDT", Side.SHORT, size=300, entry=150, mark=150, mode=CROSS)
        r = true_liquidation(long_, self.account(long_, [short], wallet=2000))
        assert r.price is not None and r.distance_pct > 0

    def test_wallet_for_target_lands_exactly_on_target(self):
        btc = pos(size=0.8, entry=64200, mark=60000, mode=CROSS)
        acct = self.account(btc, [], wallet=12000)
        needed = wallet_for_target(btc, acct, 42000)
        bumped = MarginAccount(Venue.BINANCE, needed, acct.positions, acct.schedules)
        assert true_liquidation(btc, bumped).price == pytest.approx(42000)

    def test_cross_without_account_trusts_the_exchange(self):
        btc = pos(size=0.8, entry=64200, mark=60000, mode=CROSS, liq=51000)
        ctx = RiskContext()
        assert not ctx.computable(btc)
        assert ctx.effective_distance(btc) == pytest.approx(btc.distance_to_liquidation_pct)

    def test_effective_distance_is_the_closer_of_exchange_and_computed(self):
        p = pos(mark=48000, wallet=5000, liq=44000)
        ctx = RiskContext()
        computed = true_liquidation(p).distance_pct
        assert computed < p.distance_to_liquidation_pct
        assert ctx.effective_distance(p) == pytest.approx(computed)


class TestHyperliquid:
    @pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
    @pytest.mark.parametrize("max_lev", [3, 20, 50])
    def test_matches_documented_formula(self, side, max_lev):
        # HL docs: liq = price - side * margin_available / size / (1 - l * side), l = 1 / (2 * max leverage),
        # margin_available = account_value - maintenance_margin_required (at the current price).
        q, e, mark, wallet = 12.0, 3150.0, 3300.0, 9000.0
        s = 1 if side == Side.LONG else -1
        maint_rate = 1 / (2 * max_lev)
        account_value = wallet + s * q * (mark - e)
        margin_available = account_value - q * mark * maint_rate
        hl = mark - s * margin_available / q / (1 - maint_rate * s)
        ours = solve_liquidation_price(side, q, e, wallet, hyperliquid_brackets(max_lev))
        assert ours == pytest.approx(hl, rel=1e-12)

    def test_account_schedule_overrides_default(self):
        eth = pos("ETH-PERP", Side.SHORT, size=12, entry=3150, mark=3150, venue=Venue.HYPERLIQUID, mode=CROSS)
        acct = MarginAccount(Venue.HYPERLIQUID, 9000, [eth], {"ETH": hyperliquid_brackets(25)})
        r = true_liquidation(eth, acct)
        assert r.maintenance_rate == pytest.approx(0.02) and not r.estimated_tiers


def test_distance_pct_signs():
    assert distance_pct(Side.LONG, 100, 90) == pytest.approx(10)
    assert distance_pct(Side.SHORT, 100, 110) == pytest.approx(10)
    assert distance_pct(Side.LONG, 100, None) == float("inf")


def test_funding_info_annualized():
    info = FundingInfo(Venue.BINANCE, "BTCUSDT", 0.0001, 8, next_funding_time=datetime.now(timezone.utc))
    assert info.annualized_pct == pytest.approx(0.0001 * 3 * 365 * 100)
