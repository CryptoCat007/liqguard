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

The run is finite (`steps` ticks) and every reminder ends with it. The owner
acknowledging, muting or dismissing any alert, or stopping the demo, mutes the
whole run: no further alert, advice or reminder event is produced for the rest
of it (the price path keeps ticking for the dashboard chart). Only a new start
clears the mute.

Event shapes (all dicts with "type" and "data"):
    status   {state, step, total_steps, muted}
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

from liqguard.ai.service import MuseService
from liqguard.alerts.escalation import ESCALATING, EscalationTracker, reminder_text, urgency
from liqguard.alerts.messages import advice_recommendation
from liqguard.i18n import Msg, has, normalize
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
    stress_band,
    true_liquidation,
)
from liqguard.risk.sessions import DEFAULT_LOW_LIQUIDITY_HOURS
from liqguard.risk.stops import DEFAULT_BUFFER_PCT

from .demo_data import CROSS_LONGS_ORDERS, DEMO_SIM_ORDERS, DEMO_WICKS, demo_clock_start

if TYPE_CHECKING:
    from liqguard.ai import MetaLLMClient
    from liqguard.protect import AuditLogEntry

logger = logging.getLogger(__name__)

MUTED_EVENTS = frozenset({"alert", "advice", "escalation"})

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
        "mixed", Msg("sim.book.mixed_title"), Msg("sim.book.mixed_desc"),
        DEFAULT_SPECS, DEFAULT_WALLETS, DEFAULT_FUNDING, DEMO_SIM_ORDERS,
    ),
    "cross_longs": Scenario(
        "cross_longs", Msg("sim.book.cross_longs_title"), Msg("sim.book.cross_longs_desc"),
        CROSS_LONGS_SPECS, {Venue.BINANCE: 9000.0}, CROSS_LONGS_FUNDING, CROSS_LONGS_ORDERS,
    ),
}
DEFAULT_SCENARIO = "mixed"


def _label(prefix: str, value) -> object:
    """`prefix.value` as a Msg when the locale has it, else the raw value (e.g. an unknown enum member)."""
    key = f"{prefix}.{value}"
    return Msg(key) if has(key) else value


def _source(source: str) -> object:
    return _label("sim.source", source)


async def _none() -> None:
    return None


def _join_labels(sources: list[str]) -> object:
    out = _source(sources[0])
    for source in sources[1:]:
        out = Msg("audit.join", a=out, b=_source(source))
    return out


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
        llm_client: "Optional[MuseService | MetaLLMClient]" = None,
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
        clock: Callable[[], float] = time.monotonic,
        open_at_warning: bool = False,
    ):
        self._init_kwargs = {k: v for k, v in locals().items() if k not in ("self", "__class__")}
        self.muse: Optional[MuseService] = MuseService.of(llm_client)
        self.open_at_warning = open_at_warning
        """The public demo opens where a position already needs attention, so the first screen shows an action."""
        self.clock = clock
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
        self._reminder_lock = asyncio.Lock()
        self.steps = steps
        self.tick_interval = tick_interval
        self.seed = seed
        self.language = normalize(language)

        self._subscribers: dict[str, EventCallback] = {}
        self._task: Optional[asyncio.Task] = None
        self._side_tasks: set[asyncio.Task] = set()
        self._muse_tasks: set[asyncio.Task] = set()
        self._muse_reported = self.muse_mode
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self.reset()

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        """Return to the opening step (0, or the first warning with `open_at_warning`) and clear history. Does not stop a running task."""
        rng = random.Random(self.seed)
        self._phases = {s.symbol: rng.uniform(0, 2 * math.pi) for s in self.specs}
        self._noise = {s.symbol: [rng.gauss(0, 0.25) for _ in range(self.steps + 1)] for s in self.specs}

        self.step = 0
        self.state = "idle"
        self.muted: Optional[str] = None
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
        if self.open_at_warning:
            self._open_at(self._warning_step())
        self.positions = self._positions_at(self.step)
        self.overview = self._analyze()
        self._record_tick()

    def _warning_step(self) -> int:
        """The first step at which a position is inside the warning tier (at most halfway through the run)."""
        for step in range(self.steps // 2 + 1):
            if any(p.distance_to_liquidation_pct <= self.threshold.warning_distance_pct for p in self._positions_at(step)):
                return step
        return self.steps // 2

    def _open_at(self, step: int) -> None:
        """Begin the book at `step`; funding settlements before it are skipped, not charged."""
        self.step = step
        now = self.sim_now
        for key, info in self.funding.items():
            t = info.next_funding_time
            while t <= now:
                t += timedelta(hours=info.interval_hours)
            self.funding[key] = dataclasses.replace(info, next_funding_time=t)

    def configure(self, threshold: Optional[UserThreshold] = None, language: Optional[str] = None) -> bool:
        """Apply the owner's thresholds/language. Ignored while a run is in progress."""
        if self.is_running:
            return False
        if threshold is not None:
            self.threshold = threshold
        if language:
            self.language = normalize(language)
        self.reset()
        return True

    def sibling(self, language: str) -> "MarketSimulation":
        """The same demo (book, clock, pacing, Muse client) as a separate run narrated in `language`."""
        return MarketSimulation(**{**self._init_kwargs, "language": language})

    def set_language(self, language: str) -> None:
        """Language for the owner's alerts and Muse text from now on; safe while running (nothing is recomputed)."""
        self.language = normalize(language)

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
        return {"sim_time": self.sim_now.isoformat(), "weekday": Msg(f"common.weekday.{self.sim_now.strftime('%A').lower()}"), **info.to_dict()}

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

    async def rescue_plan(self, symbol: str, wait: bool = True) -> Optional[dict]:
        """
        Rescue options for one demo position plus Muse's explanation of the trade-offs.
        With `wait=False` the explanation is the cached or deterministic one and
        `muse_pending` says whether asking again (with `wait`) will bring Muse's.
        """
        plan = self.build_rescue_plan(symbol)
        if plan is None:
            return None
        if wait or self.muse is None:
            return {"plan": plan.to_dict(), "explanation": await self._explain_rescue(plan), "muse_pending": False}
        instant = self.muse.rescue(plan, self.language)
        return {"plan": plan.to_dict(), "explanation": instant.result, "muse_pending": instant.pending is not None}

    async def _explain_rescue(self, plan: RescuePlan) -> Optional[dict]:
        if self.muse is None:
            return None
        try:
            return await self.muse.explain_rescue(plan, self.language)
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
            "summary": self.summary,
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
            "muse_mode": self.muse_mode,
            "demo": True,
            "scope": "public" if self.is_public else "private",
        }

    # ------------------------------------------------------------------ events

    def subscribe(self, name: str, callback: EventCallback) -> Callable[[], None]:
        """Register an async event callback under a channel name (e.g. "web")."""
        self._subscribers[name] = callback
        return lambda: self._subscribers.pop(name, None)

    async def _emit(self, event_type: str, data: Any) -> None:
        if self.muted and event_type in MUTED_EVENTS:
            return
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
        detail = Msg("audit.action", action=_label("audit.action_type", entry.action_type.value), amount=amount,
                     status=_label("audit.status", entry.status.value), reason=f" ({reason})" if reason else "")
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

    @property
    def llm(self) -> Optional["MetaLLMClient"]:
        return self.muse.llm if self.muse is not None else None

    @property
    def muse_mode(self) -> str:
        return "mock" if self.muse is None else self.muse.mode

    async def _muse_settled(self) -> None:
        """Tell viewers when Muse's status changed, so the badge only says live after a real answer."""
        mode = self.muse_mode
        if mode != self._muse_reported:
            self._muse_reported = mode
            await self._emit("muse", {"mode": mode})

    def ensure_summary(self) -> None:
        """A summary to show right away (rule-based); Muse's phrasing follows as a "summary" event."""
        if self.summary is None:
            self.summary = self._tick_summary(self.insights())
            if self.muse is not None and not self.muse.is_mock:
                self._spawn_muse(self.refresh_summary())

    async def _summary_soon(self) -> None:
        """The rule-based summary at once; with live Muse its phrasing follows as another "summary" event."""
        if self.muse is None or self.muse.is_mock:
            await self.refresh_summary()
            return
        self.summary = self._tick_summary(self.insights())
        await self._emit("summary", self.summary)
        self._spawn_muse(self.refresh_summary())
        self.warm_top()

    def warm_top(self, n: int = 2) -> None:
        """Ask Muse now about the rescue and stop-loss of triage's top picks, so opening them finds the answer cached."""
        if self.muse is None or self.muse.is_mock:
            return
        ctx = self.risk_context()
        plans, checks = [], []
        for item in self.insights().at_risk[:n]:
            pos = self._position(item.symbol)
            if pos is None:
                continue
            plans.append(self.build_rescue_plan(pos.symbol))
            checks.append(self.protection(pos, ctx).stop)
        self.muse.warm(self.language, tuple(plans), tuple(checks))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)
        return task

    def _spawn_muse(self, coro) -> asyncio.Task:
        """Muse follow-ups: a run finishes without waiting for them; `wait()` and `stop()` still cover them."""
        task = asyncio.create_task(coro)
        self._muse_tasks.add(task)
        task.add_done_callback(self._muse_tasks.discard)
        return task

    def deliver_later(self, pending: Awaitable[Any], event_type: str, build: Callable[[Any], Any]) -> asyncio.Task:
        """
        Emit `build(answer)` as `event_type` once a Muse answer computed outside
        the run arrives (dashboard requests), then report Muse's status. A None
        answer (Muse failed) emits nothing: the viewer keeps the text it has.
        """
        async def deliver() -> None:
            try:
                live = await pending
            except Exception as e:
                logger.error("Muse follow-up failed: %s", type(e).__name__)
                live = None
            if live is not None:
                await self._emit(event_type, build(live))
            await self._muse_settled()
        return self._spawn_muse(deliver())

    # ------------------------------------------------------------------ control

    async def start(self, source: str = "web") -> bool:
        """Start (or restart from step 0) the price path. Returns False if already running."""
        if self.is_running:
            return False
        self._loop = asyncio.get_running_loop()
        self.reset()
        self.state = "running"
        await self._emit("reset", self.snapshot())
        await self._add_audit("simulation_started", None, Msg("audit.started", source=_source(source)), source)
        await self._summary_soon()
        await self._check_protection()
        await self._check_portfolio()
        self._task = asyncio.create_task(self._run())
        return True

    async def _stop_escalation(self) -> None:
        task, self._escalation_task = self._escalation_task, None
        if task is asyncio.current_task():
            return
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def silence(self, source: str = "web") -> int:
        """End every pending wake-up reminder of this demo now (✅ / 😴 / Dismiss / ⏹ Stop demo, or the run ending)."""
        await self._stop_escalation()
        pending = self.escalation.pending_for(self.owner_id)
        now = self.clock()
        for ack in pending:
            self.escalation.acknowledge(self.owner_id, ack.id, now)
        if pending:
            await self._add_audit("reminders_silenced", None, Msg("audit.silenced", n=len(pending), source=_source(source)), source)
        return len(pending)

    async def mute(self, source: str = "web") -> bool:
        """
        The owner has seen this demo (✅ I'm awake / 😴 / Dismiss / ⏹): end every
        reminder and send nothing more from this run. False if it was already muted.
        """
        if self.muted:
            await self.silence(source)
            return False
        self.muted = source
        await self.silence(source)
        await self._add_audit("demo_muted", None, Msg("audit.muted", source=_source(source)), source)
        return True

    def pending_symbol(self, ack_id: str) -> Optional[str]:
        ack = next((a for a in self.escalation.pending_for(self.owner_id) if a.id == ack_id), None)
        return ack.symbol if ack else None

    async def _cancel_side_tasks(self) -> None:
        tasks = [t for t in (*self._side_tasks, *self._muse_tasks) if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self, source: str = "web") -> None:
        self.muted = self.muted or source
        await self._stop_escalation()
        if not self.is_running:
            await self.silence(source)
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        await self._cancel_side_tasks()
        await self.silence(source)
        self.state = "stopped"
        await self._add_audit("simulation_stopped", None, Msg("audit.stopped", source=_source(source)), source)
        await self._emit("status", self._status())

    async def switch_scenario(self, key: str, source: str = "web") -> bool:
        """Switch the demo book and tell every channel. False while running or for an unknown key."""
        await self._stop_escalation()
        if not self.set_scenario(key):
            return False
        await self._emit("reset", self.snapshot())
        await self._add_audit("scenario_switched", None, Msg("audit.scenario", title=SCENARIOS[key].title), source)
        await self._summary_soon()
        return True

    async def reset_and_notify(self) -> None:
        await self.stop()
        await self._stop_escalation()
        self.reset()
        await self._emit("reset", self.snapshot())
        await self._summary_soon()

    async def wait(self) -> None:
        """Wait for the run and any pending advice generation to finish."""
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._side_tasks or self._muse_tasks:
            await asyncio.gather(*self._side_tasks, *self._muse_tasks, return_exceptions=True)

    def _status(self) -> dict:
        return {"state": self.state, "step": self.step, "total_steps": self.steps, "muted": bool(self.muted)}

    def _tick_summary(self, insights: PortfolioInsights) -> Optional[dict]:
        """Rule-based summary from this step's overview and triage focus (goes out inside the tick)."""
        if self.muse is None:
            return None
        return {**self.muse.rule_summary(self.overview, self.language, insights.focus), "step": self.step}

    async def refresh_summary(self) -> Optional[dict]:
        """
        Muse's phrasing of the summary. It is computed from one step's numbers;
        if the simulation moved on while Muse was answering, the stale headline
        is dropped so the summary never quotes an older distance than the tick.
        """
        if self.muse is None:
            return None
        step, overview, focus = self.step, self.overview, self.insights().focus
        try:
            summary = await self.muse.summarize_portfolio(overview, self.language, focus)
        except Exception as e:
            logger.error("Portfolio summary failed: %s", type(e).__name__)
            return self.summary
        await self._muse_settled()
        if self.step != step or self.overview is not overview:
            return self.summary
        self.summary = {**summary, "step": step}
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
        await self._summary_soon()
        await self.silence("finish")
        self.state = "finished"
        await self._add_audit("simulation_finished", None, Msg("audit.finished"))
        await self._emit("status", self._status())

    async def _advance(self) -> None:
        """One step: settle funding due since the last step, reprice, then check warnings and alerts."""
        self.positions = self._positions_at(self.step)
        await self._settle_funding()
        self.positions = self._positions_at(self.step)
        self.overview = self._analyze()
        self.summary = self._tick_summary(self.insights())
        await self._emit("tick", self._record_tick())
        if self.muted:
            return
        await self._check_prefunding()
        await self._check_alerts()
        await self._send_due_reminders()
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
                Msg(f"audit.funding_{verb}", amount=abs(payment), rate=info.rate * 100, before=before, after=after),
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
                Msg("audit.prefunding", amount=warning.payment_usd, minutes=warning.minutes_to_settlement,
                    before=warning.distance_before_pct, after=warning.distance_after_pct),
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
        if self.muse is not None and not self.muse.is_mock:
            self._spawn_muse(self.refresh_summary())

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
            "focus": insights.focus,
            "ack_id": lead_alert.get("ack_id") if lead_alert else None,
            "channels": sorted(self._subscribers),
        }
        self.alerts.append(alert)
        await self._emit("alert", alert)
        order = " > ".join(t["symbol"] for t in items)
        await self._add_audit("alert_triage", top_pos.symbol, Msg("audit.triage", n=len(items), order=order))

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
            worst_check = max(checks, key=lambda c: c["risk_pct"] or 0)
            detail = Msg("audit.sizing", symbols=", ".join(c["symbol"] for c in checks), symbol=worst_check["symbol"],
                         risk=worst_check["risk_pct"] or 0, size=worst_check["suggested_size"] or 0)
        else:
            worst = insights.worst_stress()
            stress = worst.to_dict() if worst else None
            comps = [c for c in data["comparisons"] if worst is None or c["symbol"] in worst.symbols]
            alert.update(symbol=(worst.symbols[0] if worst else None), venue=(worst.venue if worst else None),
                         severity=band, stress=stress, concentration=data["concentration"], comparisons=comps)
            detail = (Msg("audit.stress", move=abs(worst.first_liquidation), venue=_label("common.venue", worst.venue))
                      if worst and worst.first_liquidation is not None else Msg("audit.concentrated"))
            if insights.concentration.flagged:
                detail = Msg("audit.with_concentration", detail=detail, message=insights.concentration.message())
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
            alert["explanation"] = self._explain_stop(alert, protection.stop)
            status = _label("tools.stop.status", protection.stop.status)
            detail = (Msg("audit.stop_prices", status=status, stop=protection.stop.stop_price, liq=pos.liquidation_price)
                      if protection.stop.stop_price is not None else Msg("audit.stop", status=status))
        else:
            alert["adl"] = info["adl"]
            detail = Msg("audit.adl", lights=protection.adl.lights, pnl=pos.unrealized_pnl)
        self.alerts.append(alert)
        await self._emit("alert", alert)
        await self._add_audit(f"{level}_warning", pos.symbol, detail)

    def _explain_stop(self, alert: dict, check) -> Optional[dict]:
        """The deterministic (or cached) explanation now; Muse's follows as an "alert_update"."""
        if self.muse is None or check is None:
            return None
        instant = self.muse.explain_stop_fix_now(check, self.language)
        if instant.pending is not None:
            self._spawn_muse(self._update_alert(alert, instant.pending))
        return instant.result

    async def _update_alert(self, alert: dict, pending: asyncio.Task) -> None:
        explanation = await pending
        await self._muse_settled()
        if explanation is not None and alert in self.alerts:
            alert["explanation"] = explanation
            await self._emit("alert_update", alert)

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
        if self.muted or level not in ESCALATING or not t.escalation_enabled:
            return
        text = f"{level.upper()} alert: {pos.symbol} {pos.distance_to_liquidation_pct:.1f}% from liquidation"
        ack = self.escalation.open(
            self.owner_id, pos.venue.value, pos.symbol, level, text, self.clock(),
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
        """Paces reminders between steps on the clock; every step also sends the ones already due."""
        tick = max(0.05, min(0.25, self.escalation_seconds_per_minute / 4))
        while True:
            await asyncio.sleep(tick)
            await self._send_due_reminders()
            if self.muted or (not self.escalation.active_for(self.owner_id) and not self.is_running):
                return

    async def _send_due_reminders(self) -> None:
        async with self._reminder_lock:
            now = self.clock()
            for ack in self.escalation.due(now):
                if self.muted:
                    return
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
                    "text": reminder_text(ack, n, minutes, demo=True, lang=self.language),
                    "channels": sorted(self._subscribers),
                }
                self.escalation.mark_sent(ack, now)
                self.escalation_log.append(event)
                await self._emit("escalation", event)
                await self._add_audit(
                    "escalation_reminder", ack.symbol,
                    Msg("audit.reminder", urgency=_label("audit.urgency", event["urgency"]), n=n, max=ack.max_reminders,
                        severity=_label("common.severity", ack.severity), minutes=minutes),
                )

    async def snooze(self, ack_id: str, minutes: float = 30, source: str = "web") -> Optional[dict]:
        """Hold reminders for one alert for `minutes` demo minutes."""
        ack = self.escalation.snooze(self.owner_id, ack_id, self.clock() + minutes * self.escalation_seconds_per_minute)
        if ack is None:
            return None
        await self._add_audit("alert_snoozed", ack.symbol, Msg("audit.snoozed", minutes=minutes, source=_source(source)), source)
        await self.mute(source)
        return {"ack_id": ack.id, "symbol": ack.symbol, "minutes": minutes, "source": source}

    async def acknowledge(self, ack_id: str, source: str = "web") -> Optional[dict]:
        """The owner tapped "I'm awake": stop reminders for that alert and mute the rest of this demo."""
        ack = self.escalation.acknowledge(self.owner_id, ack_id, self.clock())
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
        await self.mute(source)
        await self._emit("ack", event)
        await self._add_audit("alert_acknowledged", ack.symbol,
                              Msg("audit.acked", severity=_label("common.severity", ack.severity), source=_source(source),
                                  n=ack.reminders_sent), source)
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
            Msg("audit.alert", dist=pos.distance_to_liquidation_pct,
                rank=Msg("audit.rank", rank=priority.rank, of=priority.of) if priority else "",
                channels=Msg("audit.channels", names=_join_labels(alert["channels"])) if alert["channels"] else Msg("audit.no_channels")),
        )
        if advise:
            self._spawn(self._generate_advice(alert, pos, margin, plan))
        return alert

    async def _generate_advice(self, alert: dict, pos: Position, margin, plan: RescuePlan) -> None:
        """
        The advice card goes out at once with the deterministic (or cached) text;
        when Muse answers, the same card is re-sent as an "advice_update".
        """
        explanation, summary, mock = None, None, True
        advice_now = rescue_now = None
        if self.muse is not None:
            advice_now = self.muse.advice_now(pos, margin, self.language)
            rescue_now = self.muse.rescue(plan, self.language)
            explanation = advice_now.result.get("explanation")
            summary = advice_now.result.get("summary")
            mock = bool(advice_now.result.get("mock", self.muse.is_mock))
        if not explanation:
            explanation = AdviceCalculator.format_advice_text(margin, self.language)
        rescue_explanation = rescue_now.result if rescue_now else None

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
            "priority": alert.get("priority"),
            "handle_first": alert.get("handle_first"),
            "consolidated": alert.get("consolidated"),
            "liq_comparison": alert.get("liq_comparison"),
            "rescue": plan.to_dict(),
            "rescue_explanation": rescue_explanation,
            "mock": mock,
            "source": "muse" if not mock else (self.muse.fallback_source if self.muse is not None else "mock"),
        }
        card["recommendation"] = advice_recommendation(card)
        self.advice.append(card)
        await self._emit("advice", card)
        await self._add_audit(
            "advice_generated", pos.symbol,
            Msg("audit.advice", mock=Msg("audit.mock") if mock else "", amount=card["margin_to_add_usd"],
                pct=card["reduce_size_pct"]),
        )
        pending = [i.pending for i in (advice_now, rescue_now) if i is not None and i.pending is not None]
        if pending:
            self._spawn_muse(self._update_advice(card, explanation, advice_now, rescue_now))

    async def _update_advice(self, card: dict, explanation: str, advice_now, rescue_now) -> None:
        live_advice, live_rescue = await asyncio.gather(
            *(i.pending if i is not None and i.pending is not None else _none() for i in (advice_now, rescue_now)))
        await self._muse_settled()
        if live_advice is None and live_rescue is None or card not in self.advice:
            return
        if live_advice is not None:
            card.update(summary=live_advice.get("summary"), explanation=live_advice.get("explanation") or explanation,
                        mock=False, source="muse")
        if live_rescue is not None:
            card["rescue_explanation"] = live_rescue
        card["recommendation"] = advice_recommendation(card)
        await self._emit("advice_update", card)
