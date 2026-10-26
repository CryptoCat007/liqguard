"""
Shared market simulation for demo mode.

One `MarketSimulation` instance drives the demo price path for every surface:
the web dashboard and Telegram subscribe to the same event stream, so pressing
"Simulate market" on the web (or the Telegram /demo simulation) produces the
same alerts on both at the same time.

The path is deterministic (seeded) so demo recordings are reproducible. All
numbers are computed here; the LLM only phrases explanations.

Event shapes (all dicts with "type" and "data"):
    status   {state, step, total_steps}
    tick     {step, timestamp, prices: {symbol: {...}}, overview}
    alert    {id, level, symbol, venue, side, distance, mark_price, ...}
    advice   {id, alert_id, symbol, level, explanation, margin_to_add_usd, ...}
    summary  {headline, riskiest_symbol, risk_level, ...}
    audit    {timestamp, event, symbol, detail, source}
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from liqguard.models import Position, Side, UserThreshold, Venue
from liqguard.services.advice import AdviceCalculator
from liqguard.services.portfolio import PortfolioAnalyzer

if TYPE_CHECKING:
    from liqguard.llm import MetaLLMClient
    from liqguard.services.protective_actions import AuditLogEntry, ProtectiveActionsManager

logger = logging.getLogger(__name__)

EventCallback = Callable[[dict], Awaitable[None]]

TIERS = ("warning", "critical", "emergency")


@dataclass(frozen=True)
class SimPositionSpec:
    """A demo position and the distance-to-liquidation path it follows."""
    symbol: str
    venue: Venue
    side: Side
    size: float
    entry_price: float
    liquidation_price: float
    margin: float
    leverage: float
    start_distance_pct: float
    end_distance_pct: float
    lag: float = 0.0


# Cross-venue demo book: aggregated like a real Binance + Hyperliquid user.
# End distances are chosen so the run shows all three alert tiers.
DEFAULT_SPECS: tuple[SimPositionSpec, ...] = (
    SimPositionSpec("BTCUSDT", Venue.BINANCE, Side.LONG, 0.8, 64200.0, 47000.0,
                    10720.0, 5, start_distance_pct=29.0, end_distance_pct=3.2, lag=0.0),
    SimPositionSpec("ETH", Venue.HYPERLIQUID, Side.SHORT, 12.0, 3150.0, 3900.0,
                    9150.0, 4, start_distance_pct=27.5, end_distance_pct=8.1, lag=0.15),
    SimPositionSpec("SOLUSDT", Venue.BINANCE, Side.LONG, 220.0, 148.0, 112.0,
                    6688.0, 5, start_distance_pct=26.0, end_distance_pct=15.5, lag=0.3),
)


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
        actions_manager: Optional["ProtectiveActionsManager"] = None,
        threshold: Optional[UserThreshold] = None,
        specs: tuple[SimPositionSpec, ...] = DEFAULT_SPECS,
        steps: int = 40,
        tick_interval: float = 0.75,
        seed: int = 7,
        language: str = "en",
    ):
        self.llm = llm_client
        self.threshold = threshold or UserThreshold()
        self.analyzer = PortfolioAnalyzer(self.threshold)
        self.specs = specs
        self.steps = steps
        self.tick_interval = tick_interval
        self.seed = seed
        self.language = language

        self._subscribers: dict[str, EventCallback] = {}
        self._task: Optional[asyncio.Task] = None
        self._side_tasks: set[asyncio.Task] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        if actions_manager is not None:
            actions_manager.audit.add_listener(self._on_protective_audit)

        self.reset()

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        """Return to step 0 and clear history. Does not stop a running task."""
        rng = random.Random(self.seed)
        self._phases = {s.symbol: rng.uniform(0, 2 * math.pi) for s in self.specs}
        self._noise = {s.symbol: [rng.gauss(0, 0.25) for _ in range(self.steps + 1)] for s in self.specs}

        self.step = 0
        self.state = "idle"
        self.history: dict[str, list[dict]] = {s.symbol: [] for s in self.specs}
        self.alerts: list[dict] = []
        self.advice: list[dict] = []
        self.audit: list[dict] = []
        self.summary: Optional[dict] = None
        self._fired: set[tuple[str, str]] = set()
        self._alert_seq = 0
        self.positions = self._positions_at(0)
        self.overview = self.analyzer.analyze_portfolio(self.positions)
        self._record_tick()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _distance_at(self, spec: SimPositionSpec, step: int) -> float:
        progress = (step / self.steps - spec.lag) / (1 - spec.lag) if spec.lag < 1 else 1.0
        eased = _smoothstep(progress)
        base = spec.start_distance_pct - (spec.start_distance_pct - spec.end_distance_pct) * eased
        damp = 1 - min(max(progress, 0.0), 1.0)
        wiggle = (0.9 * math.sin(step * 0.8 + self._phases[spec.symbol]) + self._noise[spec.symbol][step]) * damp
        return max(base + wiggle, 0.5)

    def _positions_at(self, step: int) -> list[Position]:
        positions = []
        for spec in self.specs:
            mark = price_at_distance(spec.side, spec.liquidation_price, self._distance_at(spec, step))
            direction = 1 if spec.side == Side.LONG else -1
            positions.append(Position(
                venue=spec.venue,
                symbol=spec.symbol,
                side=spec.side,
                size=spec.size,
                entry_price=spec.entry_price,
                mark_price=mark,
                liquidation_price=spec.liquidation_price,
                margin=spec.margin,
                leverage=spec.leverage,
                unrealized_pnl=(mark - spec.entry_price) * spec.size * direction,
            ))
        return positions

    def tier_prices(self, position: Position) -> dict[str, float]:
        """Mark prices at which each alert tier fires for `position`."""
        t = self.threshold
        return {
            "warning": price_at_distance(position.side, position.liquidation_price, t.warning_distance_pct),
            "critical": price_at_distance(position.side, position.liquidation_price, t.critical_distance_pct),
            "emergency": price_at_distance(position.side, position.liquidation_price, t.emergency_distance_pct),
        }

    def _record_tick(self) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        prices = {}
        for pos in self.positions:
            point = {
                "step": self.step,
                "timestamp": now,
                "mark_price": round(pos.mark_price, 4),
                "liquidation_price": round(pos.liquidation_price, 4),
                "distance_pct": round(pos.distance_to_liquidation_pct, 3),
            }
            self.history[pos.symbol].append(point)
            prices[pos.symbol] = {
                **point,
                "venue": pos.venue.value,
                "side": pos.side.value,
                "tier_prices": {k: round(v, 4) for k, v in self.tier_prices(pos).items()},
            }
        return {"step": self.step, "timestamp": now, "prices": prices, "overview": self.overview.to_dict()}

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
            "channels": sorted(self._subscribers),
            "muse_mode": "mock" if (self.llm is None or self.llm.is_mock) else "live",
            "demo": True,
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

    def _on_protective_audit(self, entry: "AuditLogEntry") -> None:
        """Mirror protective-action audit entries (sanitized: no user ids, no payloads)."""
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
        self._task = asyncio.create_task(self._run())
        return True

    async def stop(self, source: str = "web") -> None:
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
        self.reset()
        await self._emit("reset", self.snapshot())

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
            self.positions = self._positions_at(self.step)
            self.overview = self.analyzer.analyze_portfolio(self.positions)
            await self._emit("tick", self._record_tick())
            await self._check_alerts()
        self.state = "finished"
        await self._add_audit("simulation_finished", None, "Price path complete; no execution performed")
        await self._emit("status", self._status())

    async def _check_alerts(self) -> None:
        thresholds = self._thresholds()
        fired_any = False
        for pos in self.positions:
            distance = pos.distance_to_liquidation_pct
            for tier in TIERS:
                key = (pos.symbol, tier)
                if distance <= thresholds[tier] and key not in self._fired:
                    self._fired.add(key)
                    await self._fire_alert(tier, pos)
                    fired_any = True
        if fired_any:
            self._spawn(self.refresh_summary())

    async def _fire_alert(self, level: str, pos: Position) -> None:
        self._alert_seq += 1
        margin = AdviceCalculator.calculate_margin_to_add(pos, self.threshold.safety_target_pct)
        reduction = AdviceCalculator.calculate_position_reduction(pos, self.threshold.safety_target_pct)
        alert = {
            "id": f"al_{self._alert_seq:03d}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "step": self.step,
            "level": level,
            "symbol": pos.symbol,
            "venue": pos.venue.value,
            "side": pos.side.value,
            "leverage": pos.leverage,
            "distance_pct": round(pos.distance_to_liquidation_pct, 2),
            "threshold_pct": self._thresholds()[level],
            "mark_price": round(pos.mark_price, 4),
            "liquidation_price": round(pos.liquidation_price, 4),
            "channels": sorted(self._subscribers),
        }
        self.alerts.append(alert)
        await self._emit("alert", alert)
        await self._add_audit(
            f"alert_{level}", pos.symbol,
            f"{pos.distance_to_liquidation_pct:.1f}% from liquidation → sent to {', '.join(alert['channels']) or 'no channels'}",
        )
        self._spawn(self._generate_advice(alert, pos, margin, reduction))

    async def _generate_advice(self, alert: dict, pos: Position, margin, reduction) -> None:
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
            "reduce_size_pct": round(reduction.reduce_size_pct or 0, 1),
            "reduce_size_by": round(reduction.reduce_size_by or 0, 4),
            "target_distance_pct": self.threshold.safety_target_pct,
            "mock": mock,
        }
        self.advice.append(card)
        await self._emit("advice", card)
        await self._add_audit(
            "advice_generated", pos.symbol,
            f"Muse{' (mock)' if mock else ''}: add ${card['margin_to_add_usd']:,.2f} or reduce {card['reduce_size_pct']:.1f}% "
            f"— suggestion only, confirmation required in Telegram",
        )
