"""
Demo market simulation.

Each `MarketSimulation` belongs to one owner: a Telegram user id, or `None`
for the public demo page. The owner's web dashboard and Telegram chat
subscribe to the same event stream, so "Simulate market" on the web (or
/demo in Telegram) produces the same alerts on both, and only for that
owner. The public simulation has no Telegram delivery at all.

The path is deterministic (seeded) so demo recordings are reproducible. All
numbers are computed here; the LLM only phrases explanations.

The demo book is margined like a real one: BTC and SOL share a Binance cross
wallet, ETH sits in a Hyperliquid cross account, and a TSLA stock perp sits in
isolated margin on Binance. Liquidation prices are
computed (`risk.true_liquidation`) every tick from the wallet, the other
positions' PnL and maintenance tiers. A simulated clock (12 min per step, so
a 40-step run covers 8 h) drives demo funding settlements that are paid out of
the wallets, pre-funding warnings, and rescue plans. The clock starts on a
Saturday at 20:00 UTC, so the adaptive tiers widen for the weekend, then again
for the thin 21:00–01:00 UTC hours, and TSLA's for the closed US stock market.

Each tick also runs the protection checks on demo orders and demo wick
statistics: stop-loss sanity (BTC's stop sits beyond liquidation, ETH has
none, SOL's is overtaken by its rising liquidation price), the ADL light of
the profitable TSLA position, and wake-up escalation of critical/emergency
alerts (demo time: one escalation minute lasts `escalation_seconds_per_minute`
seconds of wall time).

Event shapes (all dicts with "type" and "data"):
    status   {state, step, total_steps}
    tick     {step, timestamp, sim_time, prices: {symbol: {...}}, overview, wallets}
    alert    {id, level, symbol, venue, side, distance, mark_price, ...}
             levels: warning/critical/emergency (with "priority"), "funding" (pre-funding),
             "stop" (stop-loss check), "adl", "triage" (several positions crossed tiers in one
             step: one consolidated alert, most urgent first), "sizing" (oversized positions),
             "correlation" (concentrated book / account liquidated by a small correlated move)
    escalation {id, ack_id, symbol, severity, reminder, max_reminders, urgency, text, ...}
    ack      {ack_id, symbol, severity, reminders_sent, source}
    advice   {id, alert_id, symbol, level, explanation, margin_to_add_usd, rescue, rescue_explanation, ...}
    funding  {sim_time, symbol, venue, rate, payment_usd, wallet_after, liquidation_before, liquidation_after}
    summary  {headline, riskiest_symbol, risk_level, ...}
    audit    {timestamp, event, symbol, detail, source}
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from liqguard.alerts.escalation import ESCALATING, EscalationTracker, reminder_text, urgency
from liqguard.models import (
    CROSS,
    ISOLATED,
    FundingInfo,
    MarginAccount,
    OpenOrder,
    Position,
    Side,
    UserThreshold,
    Venue,
    WickProfile,
)
from liqguard.risk import (
    AdviceCalculator,
    FeeSchedule,
    PortfolioAnalyzer,
    PortfolioInsights,
    PositionProtection,
    RescuePlan,
    RescueSimulator,
    RiskContext,
    adaptive_threshold,
    default_brackets,
    factor,
    funding_payment,
    market_session,
    portfolio_insights,
    position_protection,
    pre_funding_warning,
    true_liquidation,
)
from liqguard.risk.sessions import DEFAULT_LOW_LIQUIDITY_HOURS
from liqguard.risk.stops import DEFAULT_BUFFER_PCT

from .demo_data import CROSS_LONGS_ORDERS, DEMO_SIM_ORDERS, DEMO_WICKS, demo_clock_start

if TYPE_CHECKING:
    from liqguard.ai import MetaLLMClient
    from liqguard.protect import AuditLogEntry

logger = logging.getLogger(__name__)

EventCallback = Callable[[dict], Awaitable[None]]

TIERS = ("warning", "critical", "emergency")


SIM_MINUTES_PER_STEP = 12
PREFUNDING_LEAD_MINUTES = 30
ESCALATION_SECONDS_PER_MINUTE = 1.5


@dataclass(frozen=True)
class SimPositionSpec:
    """
    A demo position and the path it follows.

    In a cross account all positions share one margin buffer, so their
    distances are not independent: one position per account follows a
    distance path ("driver"), the others follow a price path (`end_price_pct`,
    the mark's total move over the run) and their distance is whatever the
    shared wallet implies.
    """
    symbol: str
    venue: Venue
    side: Side
    size: float
    entry_price: float
    margin: float
    leverage: float
    start_distance_pct: float = 0.0
    end_distance_pct: float = 0.0
    lag: float = 0.0
    end_price_pct: Optional[float] = None
    margin_mode: str = CROSS
    start_price_pct: float = 0.0
    adl_quantiles: Optional[tuple[int, int]] = None

    @property
    def is_driver(self) -> bool:
        return self.end_price_pct is None


@dataclass(frozen=True)
class SimFundingSpec:
    """Demo funding schedule (labelled "demo"; live rates come from the exchanges)."""
    symbol: str
    venue: Venue
    rate: float
    interval_hours: float
    first_settlement_minutes: float


# Cross-margined Binance + Hyperliquid demo book. End distances are chosen so a
# run shows all three alert tiers, and SOL (which only drifts 5%) is dragged
# toward liquidation by BTC through the shared wallet.
DEFAULT_SPECS: tuple[SimPositionSpec, ...] = (
    SimPositionSpec("BTCUSDT", Venue.BINANCE, Side.LONG, 0.8, 64200.0, 10272.0, 5,
                    start_distance_pct=29.0, end_distance_pct=3.2, lag=0.0, adl_quantiles=(1, 1)),
    SimPositionSpec("ETH", Venue.HYPERLIQUID, Side.SHORT, 12.0, 3150.0, 9450.0, 4,
                    start_distance_pct=27.5, end_distance_pct=8.1, lag=0.15),
    SimPositionSpec("SOLUSDT", Venue.BINANCE, Side.LONG, 150.0, 148.0, 4440.0, 5,
                    end_price_pct=-5.0, lag=0.3, adl_quantiles=(0, 0)),
    # Profitable stock perp in isolated margin: far from liquidation, but its ADL light climbs to 5/5.
    SimPositionSpec("TSLAUSDT", Venue.BINANCE, Side.LONG, 40.0, 248.0, 3306.67, 3,
                    start_price_pct=4.0, end_price_pct=9.0, margin_mode=ISOLATED, adl_quantiles=(2, 4)),
)

DEFAULT_WALLETS: dict[Venue, float] = {Venue.BINANCE: 15500.0, Venue.HYPERLIQUID: 11300.0}

# BTC's 8 h settlement lands near the end of the run, when it is close to
# liquidation, so the run shows a pre-funding warning; SOL settles every 4 h and
# ETH hourly (shorts pay when the rate is negative).
DEFAULT_FUNDING: tuple[SimFundingSpec, ...] = (
    SimFundingSpec("BTCUSDT", Venue.BINANCE, 0.0025, 8, first_settlement_minutes=456),
    SimFundingSpec("SOLUSDT", Venue.BINANCE, 0.0003, 4, first_settlement_minutes=216),
    SimFundingSpec("ETH", Venue.HYPERLIQUID, -0.00003, 1, first_settlement_minutes=36),
)


# "5 longs, one wallet": five correlated longs share one Binance cross wallet
# and fall together (each by its beta × an ~8% market drop). Every platform
# liquidation line looks 50%+ away at the start, yet the account is liquidated
# by an ~8.8% correlated move; the run ends just short of it.
CROSS_LONGS_MARKET_MOVE_PCT = -8.0
CROSS_LONGS_SPECS: tuple[SimPositionSpec, ...] = tuple(
    SimPositionSpec(symbol, Venue.BINANCE, Side.LONG, size, entry, size * entry / 10, 10,
                    end_price_pct=round(factor(symbol).beta * CROSS_LONGS_MARKET_MOVE_PCT, 2), lag=lag)
    for symbol, size, entry, lag in (
        ("BTCUSDT", 0.25, 64000.0, 0.0),
        ("ETHUSDT", 5.0, 3150.0, 0.02),
        ("SOLUSDT", 100.0, 148.0, 0.04),
        ("DOGEUSDT", 90000.0, 0.165, 0.06),
        ("AVAXUSDT", 500.0, 29.0, 0.08),
    )
)
CROSS_LONGS_FUNDING: tuple[SimFundingSpec, ...] = (
    SimFundingSpec("BTCUSDT", Venue.BINANCE, 0.0001, 8, first_settlement_minutes=300),
    SimFundingSpec("SOLUSDT", Venue.BINANCE, 0.0002, 4, first_settlement_minutes=180),
)


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    description: str
    specs: tuple[SimPositionSpec, ...]
    wallets: dict
    funding: tuple[SimFundingSpec, ...]
    orders: tuple[OpenOrder, ...]

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "description": self.description,
                "symbols": [s.symbol for s in self.specs]}


SCENARIOS: dict[str, Scenario] = {
    "mixed": Scenario(
        "mixed", "Mixed book",
        "BTC + SOL cross on Binance, an ETH short on Hyperliquid and an isolated TSLA stock perp.",
        DEFAULT_SPECS, DEFAULT_WALLETS, DEFAULT_FUNDING, DEMO_SIM_ORDERS,
    ),
    "cross_longs": Scenario(
        "cross_longs", "5 longs, one wallet",
        "BTC, ETH, SOL, DOGE and AVAX longs share one Binance cross wallet and fall together.",
        CROSS_LONGS_SPECS, {Venue.BINANCE: 9000.0}, CROSS_LONGS_FUNDING, CROSS_LONGS_ORDERS,
    ),
}
DEFAULT_SCENARIO = "mixed"

# Account-level liquidation move (correlated market %) that raises the "correlation" alert to each level.
STRESS_BANDS: tuple[tuple[str, float], ...] = (("emergency", 2.5), ("critical", 5.0), ("warning", 10.0))


def stress_band(move_pct: Optional[float]) -> Optional[str]:
    if move_pct is None:
        return None
    return next((name for name, limit in STRESS_BANDS if abs(move_pct) <= limit), None)


def price_at_distance(side: Side, liquidation_price: float, distance_pct: float) -> float:
    """Mark price at which a position sits `distance_pct` from liquidation."""
    if side == Side.LONG:
        return liquidation_price / (1 - distance_pct / 100)
    return liquidation_price / (1 + distance_pct / 100)


def _smoothstep(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


class MarketSimulation:
    """Deterministic demo price path with three-tier alerting and an event bus."""

    def __init__(
        self,
        llm_client: Optional["MetaLLMClient"] = None,
        owner_id: Optional[int] = None,
        threshold: Optional[UserThreshold] = None,
        specs: tuple[SimPositionSpec, ...] = DEFAULT_SPECS,
        steps: int = 40,
        tick_interval: float = 0.75,
        seed: int = 7,
        language: str = "en",
        wallets: Optional[dict[Venue, float]] = None,
        funding: tuple[SimFundingSpec, ...] = DEFAULT_FUNDING,
        fees: Optional[FeeSchedule] = None,
        prefunding_lead_minutes: float = PREFUNDING_LEAD_MINUTES,
        low_liquidity_hours: tuple[int, int] = DEFAULT_LOW_LIQUIDITY_HOURS,
        stop_buffer_pct: float = DEFAULT_BUFFER_PCT,
        orders: tuple[OpenOrder, ...] = DEMO_SIM_ORDERS,
        wicks: Optional[dict[str, WickProfile]] = None,
        clock_start: Optional[datetime] = None,
        escalation_seconds_per_minute: float = ESCALATION_SECONDS_PER_MINUTE,
        scenario: Optional[str] = None,
    ):
        self.llm = llm_client
        self.scenario = DEFAULT_SCENARIO if scenario is None else scenario
        self.owner_id = owner_id
        self.threshold = threshold or UserThreshold()
        self.specs = specs
        self.wallet_specs = dict(DEFAULT_WALLETS if wallets is None else wallets)
        self.funding_specs = funding
        self.fees = fees or FeeSchedule()
        self.prefunding_lead_minutes = prefunding_lead_minutes
        self.low_liquidity_hours = low_liquidity_hours
        self.stop_buffer_pct = stop_buffer_pct
        self.orders = orders
        self.wick_profiles = DEMO_WICKS if wicks is None else wicks
        if scenario is not None:
            self._apply_scenario(SCENARIOS[scenario])
        self._clock_start = clock_start
        self.escalation_seconds_per_minute = escalation_seconds_per_minute
        self.escalation = EscalationTracker()
        self._protection_cache: dict[tuple, PositionProtection] = {}
        self._escalation_task: Optional[asyncio.Task] = None
        self.steps = steps
        self.tick_interval = tick_interval
        self.seed = seed
        self.language = language

        self._subscribers: dict[str, EventCallback] = {}
        self._task: Optional[asyncio.Task] = None
        self._side_tasks: set[asyncio.Task] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self.reset()

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        """Return to step 0 and clear history. Does not stop a running task."""
        rng = random.Random(self.seed)
        self._phases = {s.symbol: rng.uniform(0, 2 * math.pi) for s in self.specs}
        self._noise = {s.symbol: [rng.gauss(0, 0.25) for _ in range(self.steps + 1)] for s in self.specs}

        self.step = 0
        self.state = "idle"
        self.clock_start = self._clock_start or demo_clock_start(datetime.now(timezone.utc))
        self.history: dict[str, list[dict]] = {s.symbol: [] for s in self.specs}
        self.alerts: list[dict] = []
        self.advice: list[dict] = []
        self.audit: list[dict] = []
        self.funding_events: list[dict] = []
        self.summary: Optional[dict] = None
        self._fired: set[tuple[str, str]] = set()
        self._alert_seq = 0
        self.escalation.clear()
        self.escalation_log: list[dict] = []
        self._stop_state: dict[str, tuple] = {}
        self._protection_cache.clear()
        self._adl_state: dict[str, int] = {}
        self._insights_cache: Optional[tuple[tuple, PortfolioInsights]] = None
        self._oversized: set[str] = set()
        self._stress_band: Optional[str] = None
        self._concentration_flagged = False
        self.wallets = dict(self.wallet_specs)
        self.funding = {
            (f.venue.value, f.symbol): FundingInfo(
                venue=f.venue, symbol=f.symbol, rate=f.rate, interval_hours=f.interval_hours,
                next_funding_time=self.clock_start + timedelta(minutes=f.first_settlement_minutes),
                fetched_at=self.clock_start, source="demo",
            )
            for f in self.funding_specs
        }
        self._marks = [self._solve_marks(step) for step in range(self.steps + 1)]
        self._settled_this_step: dict[str, float] = {}
        self.positions = self._positions_at(0)
        self.overview = self._analyze()
        self._record_tick()

    def configure(self, threshold: Optional[UserThreshold] = None, language: Optional[str] = None) -> bool:
        """Apply the owner's thresholds/language. Ignored while a run is in progress."""
        if self.is_running:
            return False
        if threshold is not None:
            self.threshold = threshold
        if language:
            self.language = language
        self.reset()
        return True

    def _apply_scenario(self, scenario: Scenario) -> None:
        self.scenario = scenario.key
        self.specs = scenario.specs
        self.wallet_specs = dict(scenario.wallets)
        self.funding_specs = scenario.funding
        self.orders = scenario.orders

    def set_scenario(self, key: str) -> bool:
        """Switch the demo book. Ignored while a run is in progress or for an unknown key."""
        if self.is_running or key not in SCENARIOS:
            return False
        self._apply_scenario(SCENARIOS[key])
        self.reset()
        return True

    @property
    def is_public(self) -> bool:
        return self.owner_id is None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _progress(self, spec: SimPositionSpec, step: int) -> float:
        return (step / self.steps - spec.lag) / (1 - spec.lag) if spec.lag < 1 else 1.0

    def _wiggle(self, spec: SimPositionSpec, step: int) -> float:
        damp = 1 - min(max(self._progress(spec, step), 0.0), 1.0)
        return (0.9 * math.sin(step * 0.8 + self._phases[spec.symbol]) + self._noise[spec.symbol][step]) * damp

    def _distance_at(self, spec: SimPositionSpec, step: int) -> float:
        eased = _smoothstep(self._progress(spec, step))
        base = spec.start_distance_pct - (spec.start_distance_pct - spec.end_distance_pct) * eased
        return max(base + self._wiggle(spec, step), 0.5)

    def _follower_mark(self, spec: SimPositionSpec, step: int) -> float:
        eased = _smoothstep(self._progress(spec, step))
        start, end = spec.start_price_pct, spec.end_price_pct or 0.0
        move_pct = start + (end - start) * eased + 0.3 * self._wiggle(spec, step)
        return spec.entry_price * (1 + move_pct / 100)

    def _adl_at(self, spec: SimPositionSpec, step: int) -> Optional[int]:
        if spec.venue != Venue.BINANCE or spec.adl_quantiles is None:
            return None
        start, end = spec.adl_quantiles
        return int(round(start + (end - start) * _smoothstep(step / self.steps)))

    @property
    def sim_now(self) -> datetime:
        return self.clock_start + timedelta(minutes=SIM_MINUTES_PER_STEP * self.step)

    def _build(self, marks: dict[str, float], wallets: dict[Venue, float]) -> tuple[list[Position], list[MarginAccount]]:
        """Positions at `marks` with computed liquidation prices, and the accounts they belong to."""
        raw = []
        for spec in self.specs:
            mark = marks[spec.symbol]
            raw.append(Position(
                venue=spec.venue,
                symbol=spec.symbol,
                side=spec.side,
                size=spec.size,
                entry_price=spec.entry_price,
                mark_price=mark,
                liquidation_price=0.0,
                margin=spec.margin,
                leverage=spec.leverage,
                unrealized_pnl=(mark - spec.entry_price) * spec.size * (1 if spec.side == Side.LONG else -1),
                margin_mode=spec.margin_mode,
                isolated_wallet=None if spec.margin_mode == CROSS else spec.margin,
            ))
        accounts = self._accounts_for(raw, wallets)
        positions = []
        for p in raw:
            account = next((a for a in accounts if a.holds(p)), None)
            liq = true_liquidation(p, account).price
            positions.append(dataclasses.replace(p, liquidation_price=liq or 0.0))
        return positions, self._accounts_for(positions, wallets)

    def _accounts_for(self, positions: list[Position], wallets: dict[Venue, float]) -> list[MarginAccount]:
        accounts = []
        for venue, wallet in wallets.items():
            held = [p for p in positions if p.venue == venue]
            schedules = {p.symbol: default_brackets(p.venue, p.symbol) for p in held}
            accounts.append(MarginAccount(venue, wallet, held, schedules, source="demo"))
        return accounts

    def _solve_marks(self, step: int) -> dict[str, float]:
        """
        Marks at `step` (before funding): followers take their price path; each
        driver's mark is solved so its computed distance equals its path,
        given the followers' PnL in the same wallet.
        """
        marks = {s.symbol: (s.entry_price if s.is_driver else self._follower_mark(s, step)) for s in self.specs}
        for _ in range(3):
            positions, accounts = self._build(marks, self.wallet_specs)
            for spec in self.specs:
                if not spec.is_driver:
                    continue
                pos = next(p for p in positions if p.symbol == spec.symbol)
                account = next((a for a in accounts if a.holds(pos)), None)
                liq = true_liquidation(pos, account).price
                if liq:
                    marks[spec.symbol] = price_at_distance(spec.side, liq, self._distance_at(spec, step))
        return marks

    def _positions_at(self, step: int) -> list[Position]:
        positions, self.accounts = self._build(self._marks[step], self.wallets)
        for spec, pos in zip(self.specs, positions):
            pos.adl_quantile = self._adl_at(spec, step)
        return positions

    def risk_context(self, fees: Optional[FeeSchedule] = None) -> RiskContext:
        """Accounts, demo funding, orders, wick stats and fees at the simulated time (what /risk, /positions and the dashboard use)."""
        venues = {s.venue for s in self.specs}
        return RiskContext(
            accounts=list(self.accounts),
            funding=dict(self.funding),
            fees=fees or self.fees,
            now=self.sim_now,
            orders={(v.value, "mainnet"): [o for o in self.orders if o.venue == v] for v in venues},
            wicks={
                (s.venue.value, s.symbol.removesuffix("-PERP")): self.wick_profiles[s.symbol]
                for s in self.specs if s.symbol in self.wick_profiles
            },
            low_liquidity_hours=self.low_liquidity_hours,
            stop_buffer_pct=self.stop_buffer_pct,
        )

    def protection(self, position: Position, ctx: Optional[RiskContext] = None) -> PositionProtection:
        """Cached per step and wallet state: alerts, ticks and snapshots of one step all see the same checks."""
        key = (self.step, position.symbol, round(position.mark_price, 8), tuple(sorted(self.wallets.values())), id(self.threshold))
        cached = self._protection_cache.get(key)
        if cached is None:
            if len(self._protection_cache) > 256:
                self._protection_cache.clear()
            cached = self._protection_cache[key] = position_protection(position, ctx or self.risk_context(), self.threshold)
        return cached

    def insights(self, ctx: Optional[RiskContext] = None) -> PortfolioInsights:
        """Triage, stress test, platform vs correlated liq, concentration and sizing for this step (cached)."""
        key = (self.step, tuple(round(p.mark_price, 8) for p in self.positions), tuple(sorted(self.wallets.values())), id(self.threshold))
        if self._insights_cache is not None and self._insights_cache[0] == key:
            return self._insights_cache[1]
        ctx = ctx or self.risk_context()
        protections = {(p.venue.value, p.symbol): self.protection(p, ctx) for p in self.positions}
        result = portfolio_insights(self.positions, ctx, self.threshold, protections)
        self._insights_cache = (key, result)
        return result

    def _protection_dict(self) -> dict:
        ctx = self.risk_context()
        return {p.symbol: self.protection(p, ctx).to_dict() for p in self.positions}

    def _tiers_for(self, position: Position, ctx: Optional[RiskContext] = None) -> dict[str, float]:
        ctx = ctx or self.risk_context()
        return adaptive_threshold(self.threshold, position, ctx.wicks_for(position), ctx.session(position)).tiers()

    def _session_dict(self) -> dict:
        info = market_session(self.sim_now, "", self.low_liquidity_hours)
        return {"sim_time": self.sim_now.isoformat(), "weekday": self.sim_now.strftime("%A"), **info.to_dict()}

    def _analyze(self):
        return PortfolioAnalyzer(self.threshold, self.risk_context()).analyze_portfolio(self.positions)

    def _account(self, position: Position) -> Optional[MarginAccount]:
        return next((a for a in self.accounts if a.holds(position)), None)

    def _position(self, symbol: str) -> Optional[Position]:
        return next((p for p in self.positions if p.symbol.upper() == symbol.upper()), None)

    def build_rescue_plan(self, symbol: str) -> Optional[RescuePlan]:
        position = self._position(symbol)
        if position is None:
            return None
        ctx = self.risk_context()
        simulator = RescueSimulator(self.fees, self.threshold.safety_target_pct)
        return simulator.simulate(position, ctx.account_for(position), ctx.funding_for(position), ctx.now)

    async def rescue_plan(self, symbol: str) -> Optional[dict]:
        """Rescue options for one demo position plus Muse's explanation of the trade-offs."""
        plan = self.build_rescue_plan(symbol)
        if plan is None:
            return None
        return {"plan": plan.to_dict(), "explanation": await self._explain_rescue(plan)}

    async def _explain_rescue(self, plan: RescuePlan) -> Optional[dict]:
        if self.llm is None:
            return None
        try:
            return await self.llm.explain_rescue(plan, self.language)
        except Exception as e:
            logger.error("Rescue explanation failed: %s", type(e).__name__)
            return None

    def tier_prices(self, position: Position, ctx: Optional[RiskContext] = None) -> dict[str, float]:
        """Mark prices at which each (adaptive) alert tier fires for `position`."""
        tiers = self._tiers_for(position, ctx)
        return {k: price_at_distance(position.side, position.liquidation_price, tiers[k]) for k in TIERS}

    def _record_tick(self) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        sim_time = self.sim_now.isoformat()
        ctx = self.risk_context()
        protection = {p.symbol: self.protection(p, ctx).to_dict() for p in self.positions}
        prices = {}
        for pos in self.positions:
            point = {
                "step": self.step,
                "timestamp": now,
                "sim_time": sim_time,
                "mark_price": round(pos.mark_price, 4),
                "liquidation_price": round(pos.liquidation_price, 4),
                "distance_pct": round(pos.distance_to_liquidation_pct, 3),
                "thresholds": protection[pos.symbol]["thresholds"]["effective"],
            }
            if pos.symbol in self._settled_this_step:
                point["funding_paid_usd"] = round(self._settled_this_step[pos.symbol], 2)
            self.history[pos.symbol].append(point)
            prices[pos.symbol] = {
                **point,
                "venue": pos.venue.value,
                "side": pos.side.value,
                "tier_prices": {k: round(v, 4) for k, v in self.tier_prices(pos, ctx).items()},
                "adl_quantile": pos.adl_quantile,
            }
        return {
            "step": self.step,
            "timestamp": now,
            "sim_time": sim_time,
            "prices": prices,
            "overview": self.overview.to_dict(),
            "wallets": self._wallets_dict(),
            "protection": protection,
            "session": self._session_dict(),
            "escalation": self._escalation_state(),
            "insights": self.insights(ctx).to_dict(),
            "scenario": self.scenario,
        }

    def _wallets_dict(self) -> dict:
        return {v.value: round(w, 2) for v, w in self.wallets.items()}

    def _funding_dict(self) -> dict:
        out = {}
        for pos in self.positions:
            info = self.funding.get((pos.venue.value, pos.symbol))
            if info is not None:
                out[pos.symbol] = info.to_dict()
        return out

    def _thresholds(self) -> dict[str, float]:
        return {
            "warning": self.threshold.warning_distance_pct,
            "critical": self.threshold.critical_distance_pct,
            "emergency": self.threshold.emergency_distance_pct,
        }

    def snapshot(self) -> dict:
        """Full state for a newly connected client. Contains no credentials."""
        return {
            "state": self.state,
            "step": self.step,
            "total_steps": self.steps,
            "tick_interval": self.tick_interval,
            "thresholds": self._thresholds(),
            "safety_target_pct": self.threshold.safety_target_pct,
            "overview": self.overview.to_dict(),
            "summary": self.summary,
            "history": self.history,
            "tier_prices": {p.symbol: {k: round(v, 4) for k, v in self.tier_prices(p).items()} for p in self.positions},
            "alerts": self.alerts,
            "advice": self.advice,
            "audit": self.audit,
            "funding": self._funding_dict(),
            "funding_events": self.funding_events,
            "wallets": self._wallets_dict(),
            "sim_time": self.sim_now.isoformat(),
            "minutes_per_step": SIM_MINUTES_PER_STEP,
            "protection": self._protection_dict(),
            "session": self._session_dict(),
            "escalation": self._escalation_state(),
            "escalation_log": self.escalation_log,
            "orders": [o.to_dict() for o in self.orders],
            "insights": self.insights().to_dict(),
            "risk_settings": {"risk_per_trade_pct": self.threshold.risk_per_trade_pct,
                              "max_margin_pct": self.threshold.max_margin_pct},
            "scenario": self.scenario,
            "scenarios": [sc.to_dict() for sc in SCENARIOS.values()],
            "channels": sorted(self._subscribers),
            "muse_mode": "mock" if (self.llm is None or self.llm.is_mock) else "live",
            "demo": True,
            "scope": "public" if self.is_public else "private",
        }

    # ------------------------------------------------------------------ events

    def subscribe(self, name: str, callback: EventCallback) -> Callable[[], None]:
        """Register an async event callback under a channel name (e.g. "web")."""
        self._subscribers[name] = callback
        return lambda: self._subscribers.pop(name, None)

    async def _emit(self, event_type: str, data: Any) -> None:
        event = {"type": event_type, "data": data}
        for name, callback in list(self._subscribers.items()):
            try:
                await callback(event)
            except Exception as e:
                logger.error(f"Simulation subscriber '{name}' failed: {e}")

    async def _add_audit(self, event: str, symbol: Optional[str], detail: str, source: str = "simulation") -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "symbol": symbol,
            "detail": detail,
            "source": source,
        }
        self.audit.append(entry)
        self.audit = self.audit[-200:]
        await self._emit("audit", entry)

    def record_protective_audit(self, entry: "AuditLogEntry") -> None:
        """Mirror one of the owner's protective-action audit entries (sanitized: no ids, no payloads)."""
        if entry.user_id != self.owner_id:
            return
        reason = entry.details.get("reason") if isinstance(entry.details, dict) else None
        amount = f" ${entry.amount_usd:,.2f}" if entry.amount_usd else ""
        detail = f"{entry.action_type.value}{amount} → {entry.status.value}" + (f" ({reason})" if reason else "")
        coro = self._add_audit(f"action_{entry.status.value}", entry.symbol, detail, source="telegram")
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            self._spawn(coro)
        elif loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            coro.close()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)
        return task

    # ------------------------------------------------------------------ control

    async def start(self, source: str = "web") -> bool:
        """Start (or restart from step 0) the price path. Returns False if already running."""
        if self.is_running:
            return False
        self._loop = asyncio.get_running_loop()
        self.reset()
        self.state = "running"
        await self._emit("reset", self.snapshot())
        await self._add_audit("simulation_started", None, f"Demo price path started from {source}", source)
        await self.refresh_summary()
        await self._check_protection()
        await self._check_portfolio()
        self._task = asyncio.create_task(self._run())
        return True

    async def _stop_escalation(self) -> None:
        task, self._escalation_task = self._escalation_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def stop(self, source: str = "web") -> None:
        await self._stop_escalation()
        if not self.is_running:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self.state = "stopped"
        await self._add_audit("simulation_stopped", None, f"Stopped from {source}", source)
        await self._emit("status", self._status())

    async def reset_and_notify(self) -> None:
        await self.stop()
        await self._stop_escalation()
        self.reset()
        await self._emit("reset", self.snapshot())
        await self.refresh_summary()

    async def wait(self) -> None:
        """Wait for the run and any pending advice generation to finish."""
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._side_tasks:
            await asyncio.gather(*list(self._side_tasks), return_exceptions=True)

    def _status(self) -> dict:
        return {"state": self.state, "step": self.step, "total_steps": self.steps}

    async def refresh_summary(self) -> Optional[dict]:
        if self.llm is None:
            return None
        try:
            self.summary = await self.llm.summarize_portfolio(self.overview, self.language)
        except Exception as e:
            logger.error(f"Portfolio summary failed: {e}")
            return self.summary
        await self._emit("summary", self.summary)
        return self.summary

    async def _run(self) -> None:
        await self._emit("status", self._status())
        while self.step < self.steps:
            await asyncio.sleep(self.tick_interval)
            self.step += 1
            await self._advance()
        if self._side_tasks:
            await asyncio.gather(*list(self._side_tasks), return_exceptions=True)
        await self.refresh_summary()
        self.state = "finished"
        await self._add_audit("simulation_finished", None, "Price path complete; no execution performed")
        await self._emit("status", self._status())

    async def _advance(self) -> None:
        """One step: settle funding due since the last step, reprice, then check warnings and alerts."""
        self.positions = self._positions_at(self.step)
        await self._settle_funding()
        self.positions = self._positions_at(self.step)
        self.overview = self._analyze()
        await self._emit("tick", self._record_tick())
        await self._check_prefunding()
        await self._check_alerts()
        await self._check_protection()
        await self._check_portfolio()

    async def _settle_funding(self) -> None:
        start = self.clock_start + timedelta(minutes=SIM_MINUTES_PER_STEP * (self.step - 1))
        end = self.sim_now
        self._settled_this_step = {}
        for (venue, symbol), info in self.funding.items():
            t = info.next_funding_time
            if not (start < t <= end):
                continue
            pos = self._position(symbol)
            if pos is None:
                continue
            before = pos.liquidation_price
            payment = funding_payment(pos, info.rate)
            self.wallets[pos.venue] = self.wallets.get(pos.venue, 0.0) - payment
            self.funding[(venue, symbol)] = dataclasses.replace(
                info, next_funding_time=t + timedelta(hours=info.interval_hours)
            )
            self._settled_this_step[symbol] = payment
            self.positions = self._positions_at(self.step)
            after = self._position(symbol).liquidation_price
            event = {
                "sim_time": t.isoformat(),
                "step": self.step,
                "symbol": symbol,
                "venue": venue,
                "rate_pct": round(info.rate * 100, 6),
                "interval_hours": info.interval_hours,
                "payment_usd": round(payment, 2),
                "wallet_after": round(self.wallets[pos.venue], 2),
                "liquidation_before": round(before, 4),
                "liquidation_after": round(after, 4),
                "source": info.source,
            }
            self.funding_events.append(event)
            await self._emit("funding", event)
            verb = "paid" if payment > 0 else "received"
            await self._add_audit(
                "funding_settled", symbol,
                f"{verb} ${abs(payment):,.2f} funding ({info.rate * 100:+.4f}%, demo) → liq ${before:,.2f} → ${after:,.2f}",
            )

    async def _check_prefunding(self) -> None:
        ctx = self.risk_context()
        for pos in self.positions:
            warning = pre_funding_warning(
                pos, ctx.account_for(pos), ctx.funding_for, ctx.now,
                self.protection(pos, ctx).adaptive.threshold, self.prefunding_lead_minutes,
            )
            if warning is None:
                continue
            key = (pos.symbol, f"funding@{warning.settlement_time.isoformat()}")
            if key in self._fired:
                continue
            self._fired.add(key)
            self._alert_seq += 1
            alert = {
                "id": f"al_{self._alert_seq:03d}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sim_time": ctx.now.isoformat(),
                "step": self.step,
                "level": "funding",
                "symbol": pos.symbol,
                "venue": pos.venue.value,
                "side": pos.side.value,
                "leverage": pos.leverage,
                "distance_pct": round(pos.distance_to_liquidation_pct, 2),
                "threshold_pct": round(self.protection(pos, ctx).adaptive.tiers()["warning"], 2),
                "mark_price": round(pos.mark_price, 4),
                "liquidation_price": round(pos.liquidation_price, 4),
                "funding": warning.to_dict(),
                "channels": sorted(self._subscribers),
            }
            self.alerts.append(alert)
            await self._emit("alert", alert)
            await self._add_audit(
                "prefunding_warning", pos.symbol,
                f"pays ${warning.payment_usd:,.2f} in {warning.minutes_to_settlement:.0f} min → "
                f"distance {warning.distance_before_pct:.1f}% → {warning.distance_after_pct:.1f}%",
            )

    async def _check_alerts(self) -> None:
        """
        Tier crossings. When two or more positions cross tiers in the same
        step, they go out as one consolidated "triage" alert, most urgent
        first; only the top one gets escalation and Muse advice.
        """
        ctx = self.risk_context()
        insights = self.insights(ctx)
        crossings: list[tuple[str, Position, PositionProtection]] = []
        for pos in self.positions:
            distance = pos.distance_to_liquidation_pct
            protection = self.protection(pos, ctx)
            thresholds = protection.adaptive.tiers()
            self.escalation.update(self.owner_id, pos.venue.value, pos.symbol, distance)
            for tier in TIERS:
                key = (pos.symbol, tier)
                if distance <= thresholds[tier] and key not in self._fired:
                    self._fired.add(key)
                    crossings.append((tier, pos, protection))
        if not crossings:
            return
        symbols = list(dict.fromkeys(pos.symbol for _, pos, _ in crossings))
        if len(symbols) < 2:
            for tier, pos, protection in crossings:
                await self._fire_alert(tier, pos, protection, insights)
        else:
            await self._fire_triage(crossings, insights)
        self._spawn(self.refresh_summary())

    async def _fire_triage(self, crossings: list, insights: PortfolioInsights) -> None:
        self._alert_seq += 1
        triage_id = f"al_{self._alert_seq:03d}"
        top = insights.top
        worst: dict[str, tuple[str, Position, PositionProtection]] = {}
        for tier, pos, protection in crossings:
            worst[pos.symbol] = (tier, pos, protection)
        tier_alerts = []
        for tier, pos, protection in crossings:
            lead = top is not None and pos.symbol == top.symbol and worst[pos.symbol][0] == tier
            tier_alerts.append(await self._fire_alert(tier, pos, protection, insights, consolidated=triage_id, advise=lead))
        lead_alert = next((a for a in tier_alerts if a.get("ack_id")), None)
        items = [t.to_dict() for t in insights.at_risk]
        severity = TIERS[max(TIERS.index(t["severity"]) for t in items)] if items else crossings[-1][0]
        top_pos = self._position(top.symbol) if top else crossings[0][1]
        alert = {
            "id": triage_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sim_time": self.sim_now.isoformat(),
            "step": self.step,
            "level": "triage",
            "severity": severity,
            "symbol": top_pos.symbol,
            "venue": top_pos.venue.value,
            "side": top_pos.side.value,
            "distance_pct": round(top_pos.distance_to_liquidation_pct, 2),
            "mark_price": round(top_pos.mark_price, 4),
            "liquidation_price": round(top_pos.liquidation_price, 4),
            "crossed": [{"symbol": pos.symbol, "tier": tier} for tier, pos, _ in crossings],
            "items": items,
            "handle_first": top.to_dict() if top else None,
            "ack_id": lead_alert.get("ack_id") if lead_alert else None,
            "channels": sorted(self._subscribers),
        }
        self.alerts.append(alert)
        await self._emit("alert", alert)
        order = " > ".join(t["symbol"] for t in items)
        await self._add_audit("alert_triage", top_pos.symbol, f"{len(items)} positions at risk, handle in order: {order}")

    async def _check_portfolio(self) -> None:
        """Sizing and correlation/stress warnings, once per state change (like the live monitor)."""
        insights = self.insights()
        oversized = {s.symbol for s in insights.oversized}
        new = oversized - self._oversized
        self._oversized = oversized
        if new:
            await self._fire_portfolio_alert("sizing", insights, sorted(new))
        worst = insights.worst_stress()
        band = stress_band(worst.first_liquidation if worst else None)
        flagged = insights.concentration.flagged
        worse = band is not None and (self._stress_band is None or TIERS.index(band) > TIERS.index(self._stress_band))
        if worse or (flagged and not self._concentration_flagged):
            await self._fire_portfolio_alert("correlation", insights, band=band)
        if worse:
            self._stress_band = band
        self._concentration_flagged = flagged

    async def _fire_portfolio_alert(self, level: str, insights: PortfolioInsights,
                                    symbols: Optional[list[str]] = None, band: Optional[str] = None) -> None:
        self._alert_seq += 1
        data = insights.to_dict()
        alert = {
            "id": f"al_{self._alert_seq:03d}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sim_time": self.sim_now.isoformat(),
            "step": self.step,
            "level": level,
            "channels": sorted(self._subscribers),
        }
        if level == "sizing":
            checks = [s for s in data["sizing"] if s["symbol"] in symbols]
            alert.update(symbol=checks[0]["symbol"], venue=checks[0]["venue"], side=checks[0]["side"],
                         symbols=symbols, sizing=checks)
            detail = "oversized for the account: " + ", ".join(
                f"{c['symbol']} (risk {c['risk_pct']:.1f}% of equity, suggest {c['suggested_size']:g})" for c in checks)
        else:
            worst = insights.worst_stress()
            stress = worst.to_dict() if worst else None
            comps = [c for c in data["comparisons"] if worst is None or c["symbol"] in worst.symbols]
            alert.update(symbol=(worst.symbols[0] if worst else None), venue=(worst.venue if worst else None),
                         severity=band, stress=stress, concentration=data["concentration"], comparisons=comps)
            detail = (f"a {abs(worst.first_liquidation):.1f}% correlated market move liquidates the {worst.venue} account"
                      if worst and worst.first_liquidation is not None else "concentrated book")
            if insights.concentration.flagged:
                detail += f"; {insights.concentration.message()}"
        self.alerts.append(alert)
        await self._emit("alert", alert)
        await self._add_audit(f"{level}_warning", alert.get("symbol"), detail)

    async def _check_protection(self) -> None:
        """Stop-loss sanity and ADL, once per state change per position (like the live monitor)."""
        ctx = self.risk_context()
        for pos in self.positions:
            protection = self.protection(pos, ctx)
            check = protection.stop
            if check is not None and check.needs_warning:
                state = (check.status, None if check.stop_price is None else round(check.stop_price, 6))
                if self._stop_state.get(pos.symbol) != state:
                    self._stop_state[pos.symbol] = state
                    await self._fire_protection_alert("stop", pos, protection, ctx)
            elif check is not None:
                self._stop_state.pop(pos.symbol, None)
            adl = protection.adl
            if adl.warn and self._adl_state.get(pos.symbol, 0) < adl.lights:
                self._adl_state[pos.symbol] = adl.lights
                await self._fire_protection_alert("adl", pos, protection, ctx)

    async def _fire_protection_alert(self, level: str, pos: Position, protection: PositionProtection, ctx: RiskContext) -> None:
        self._alert_seq += 1
        info = protection.to_dict()
        alert = {
            "id": f"al_{self._alert_seq:03d}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sim_time": ctx.now.isoformat(),
            "step": self.step,
            "level": level,
            "symbol": pos.symbol,
            "venue": pos.venue.value,
            "side": pos.side.value,
            "leverage": pos.leverage,
            "distance_pct": round(pos.distance_to_liquidation_pct, 2),
            "mark_price": round(pos.mark_price, 4),
            "liquidation_price": round(pos.liquidation_price, 4),
            "unrealized_pnl": round(pos.unrealized_pnl, 2),
            "channels": sorted(self._subscribers),
        }
        if level == "stop":
            alert["stop"] = info["stop"]
            alert["explanation"] = await self._explain_stop(protection.stop)
            detail = f"stop check: {protection.stop.status.replace('_', ' ')}"
            if protection.stop.stop_price is not None:
                detail += f" (stop ${protection.stop.stop_price:,.2f} vs liq ${pos.liquidation_price:,.2f})"
        else:
            alert["adl"] = info["adl"]
            detail = f"ADL {protection.adl.lights}/5 on a profitable position (+${pos.unrealized_pnl:,.2f})"
        self.alerts.append(alert)
        await self._emit("alert", alert)
        await self._add_audit(f"{level}_warning", pos.symbol, detail)

    async def _explain_stop(self, check) -> Optional[dict]:
        if self.llm is None or check is None:
            return None
        try:
            return await self.llm.explain_stop_fix(check, self.language)
        except Exception as e:
            logger.error("Stop-loss explanation failed: %s", type(e).__name__)
            return None

    # ------------------------------------------------------------------ escalation

    def _escalation_state(self) -> dict:
        t = self.threshold
        return {
            "enabled": t.escalation_enabled,
            "interval_minutes": t.escalation_interval_minutes,
            "max_reminders": t.escalation_max_reminders,
            "seconds_per_minute": self.escalation_seconds_per_minute,
            "pending": [a.to_dict() for a in self.escalation.pending_for(self.owner_id)],
        }

    def _open_escalation(self, level: str, pos: Position, alert: dict) -> None:
        t = self.threshold
        if level not in ESCALATING or not t.escalation_enabled:
            return
        text = f"{level.upper()} alert: {pos.symbol} {pos.distance_to_liquidation_pct:.1f}% from liquidation"
        ack = self.escalation.open(
            self.owner_id, pos.venue.value, pos.symbol, level, text, time.monotonic(),
            interval_seconds=t.escalation_interval_minutes * self.escalation_seconds_per_minute,
            max_reminders=t.escalation_max_reminders, distance_pct=pos.distance_to_liquidation_pct,
            meta={"alert_id": alert["id"]},
        )
        if ack is None:
            return
        alert["ack_id"] = ack.id
        if self._escalation_task is None or self._escalation_task.done():
            self._escalation_task = asyncio.create_task(self._escalation_loop())

    async def _escalation_loop(self) -> None:
        tick = max(0.05, min(0.25, self.escalation_seconds_per_minute / 4))
        while True:
            await asyncio.sleep(tick)
            now = time.monotonic()
            for ack in self.escalation.due(now):
                n = ack.reminders_sent + 1
                minutes = (now - ack.opened_at) / self.escalation_seconds_per_minute
                event = {
                    "id": f"esc_{len(self.escalation_log) + 1:03d}",
                    "ack_id": ack.id,
                    "alert_id": ack.meta.get("alert_id"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sim_time": self.sim_now.isoformat(),
                    "symbol": ack.symbol,
                    "venue": ack.venue,
                    "severity": ack.severity,
                    "reminder": n,
                    "max_reminders": ack.max_reminders,
                    "urgency": urgency(n, ack.max_reminders),
                    "minutes_since_alert": round(minutes, 1),
                    "distance_pct": None if ack.distance_pct is None else round(ack.distance_pct, 2),
                    "text": reminder_text(ack, n, minutes, demo=True),
                    "channels": sorted(self._subscribers),
                }
                self.escalation.mark_sent(ack, now)
                self.escalation_log.append(event)
                await self._emit("escalation", event)
                await self._add_audit(
                    "escalation_reminder", ack.symbol,
                    f"{event['urgency']} {n}/{ack.max_reminders}: {ack.severity} alert unacknowledged for {minutes:.0f} demo min",
                )
            if not self.escalation.active_for(self.owner_id) and not self.is_running:
                return

    async def acknowledge(self, ack_id: str, source: str = "web") -> Optional[dict]:
        """The owner tapped "I'm awake": stop reminders for that alert."""
        ack = self.escalation.acknowledge(self.owner_id, ack_id, time.monotonic())
        if ack is None:
            return None
        event = {
            "ack_id": ack.id,
            "alert_id": ack.meta.get("alert_id"),
            "symbol": ack.symbol,
            "severity": ack.severity,
            "reminders_sent": ack.reminders_sent,
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._emit("ack", event)
        await self._add_audit("alert_acknowledged", ack.symbol,
                              f"{ack.severity} alert acknowledged from {source} after {ack.reminders_sent} reminder(s)", source)
        return event

    async def _fire_alert(self, level: str, pos: Position, protection: Optional[PositionProtection] = None,
                          insights: Optional[PortfolioInsights] = None, consolidated: Optional[str] = None,
                          advise: bool = True) -> dict:
        self._alert_seq += 1
        protection = protection or self.protection(pos)
        insights = insights or self.insights()
        priority = insights.priority(pos)
        top = insights.top
        plan = self.build_rescue_plan(pos.symbol)
        margin = plan.margin_advice()
        alert = {
            "id": f"al_{self._alert_seq:03d}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sim_time": self.sim_now.isoformat(),
            "step": self.step,
            "level": level,
            "symbol": pos.symbol,
            "venue": pos.venue.value,
            "side": pos.side.value,
            "leverage": pos.leverage,
            "distance_pct": round(pos.distance_to_liquidation_pct, 2),
            "threshold_pct": round(protection.adaptive.tiers()[level], 2),
            "base_threshold_pct": self._thresholds()[level],
            "adaptive_reasons": protection.adaptive.reasons,
            "wicks": protection.to_dict()["wicks"],
            "mark_price": round(pos.mark_price, 4),
            "liquidation_price": round(pos.liquidation_price, 4),
            "priority": priority.to_dict() if priority else None,
            "handle_first": top.to_dict() if top else None,
            "channels": sorted(self._subscribers),
        }
        comparison = insights.comparison(pos)
        if comparison is not None and comparison.correlated_market_move_pct is not None:
            alert["liq_comparison"] = comparison.to_dict()
        if consolidated:
            alert["consolidated"] = consolidated
        if advise:
            self._open_escalation(level, pos, alert)
        self.alerts.append(alert)
        await self._emit("alert", alert)
        await self._add_audit(
            f"alert_{level}", pos.symbol,
            f"{pos.distance_to_liquidation_pct:.1f}% from liquidation"
            + (f" (#{priority.rank} of {priority.of})" if priority else "")
            + f" → sent to {', '.join(alert['channels']) or 'no channels'}",
        )
        if advise:
            self._spawn(self._generate_advice(alert, pos, margin, plan))
        return alert

    async def _generate_advice(self, alert: dict, pos: Position, margin, plan: RescuePlan) -> None:
        explanation, summary, mock = None, None, True
        if self.llm is not None:
            try:
                result = await self.llm.explain_risk_with_advice(pos, margin, self.language)
                explanation = result.get("explanation")
                summary = result.get("summary")
                mock = bool(result.get("mock", self.llm.is_mock))
            except Exception as e:
                logger.error(f"Advice generation failed: {e}")
        if not explanation:
            explanation = AdviceCalculator.format_advice_text(margin, self.language)
        rescue_explanation = await self._explain_rescue(plan)

        card = {
            "id": f"adv_{alert['id']}",
            "alert_id": alert["id"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": alert["level"],
            "symbol": pos.symbol,
            "venue": pos.venue.value,
            "side": pos.side.value,
            "distance_pct": alert["distance_pct"],
            "mark_price": alert["mark_price"],
            "liquidation_price": alert["liquidation_price"],
            "summary": summary,
            "explanation": explanation,
            "margin_to_add_usd": round(margin.margin_to_add_usd or 0, 2),
            "new_liquidation_price": round(margin.new_liquidation_price, 4) if margin.new_liquidation_price else None,
            "reduce_size_pct": round(margin.reduce_size_pct or 0, 1),
            "reduce_size_by": round(margin.reduce_size_by or 0, 4),
            "target_distance_pct": self.threshold.safety_target_pct,
            "margin_mode": plan.margin_mode,
            "ack_id": alert.get("ack_id"),
            "threshold_pct": alert.get("threshold_pct"),
            "adaptive_reasons": alert.get("adaptive_reasons", []),
            "wicks": alert.get("wicks"),
            "rescue": plan.to_dict(),
            "rescue_explanation": rescue_explanation,
            "mock": mock,
        }
        self.advice.append(card)
        await self._emit("advice", card)
        await self._add_audit(
            "advice_generated", pos.symbol,
            f"Muse{' (mock)' if mock else ''}: add ${card['margin_to_add_usd']:,.2f} or reduce {card['reduce_size_pct']:.1f}% "
            f"— suggestion only, confirmation required in Telegram",
        )
