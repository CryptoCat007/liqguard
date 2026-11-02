"""Where a user's positions come from: their simulation, their demo book, or their own exchange bindings."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from liqguard.exchanges import DemoAdapter, ExchangeAdapter, UnsupportedBinding, adapter_for_binding
from liqguard.models import (
    ExchangeBinding,
    FundingInfo,
    MarginAccount,
    OpenOrder,
    Position,
    User,
    Venue,
    WickProfile,
)
from liqguard.risk import FeeSchedule, RiskContext
from liqguard.simulation import DEMO_WICKS, SimulationRegistry

from .market import MarketDataService
from .wicks import WickService

logger = logging.getLogger(__name__)

AdapterFactory = Callable[[ExchangeBinding], ExchangeAdapter]

LABEL_SIMULATION = "📊 Demo Mode · market simulation"
LABEL_DEMO = "📊 Demo Mode"
LABEL_LIVE = "📊 Live Positions"
BOOK_LABEL = {LABEL_SIMULATION: "book.simulation", LABEL_DEMO: "book.demo", LABEL_LIVE: "book.live"}
"""Locale key for each book label, so every channel shows it in the reader's language."""


@dataclass
class BookSnapshot:
    """One user's positions plus the margin accounts and funding the exact risk math needs."""

    positions: list[Position]
    label: str
    context: RiskContext = field(default_factory=RiskContext)


class PositionService:
    """
    Resolves positions for exactly one user at a time.

    Demo books are per user; live positions are fetched with adapters built
    from that user's own decrypted bindings and closed right after use.
    """

    def __init__(
        self,
        simulations: SimulationRegistry,
        adapter_factory: AdapterFactory = adapter_for_binding,
        market: Optional[MarketDataService] = None,
        fees: Optional[FeeSchedule] = None,
        wicks: Optional[WickService] = None,
        low_liquidity_hours: Optional[tuple[int, int]] = None,
        stop_buffer_pct: Optional[float] = None,
        demo_seed: Optional[int] = None,
    ):
        self.simulations = simulations
        self.demo_seed = demo_seed
        self._adapter_factory = adapter_factory
        self.market = market
        self.fees = fees or FeeSchedule()
        self.wicks = wicks
        self._context_options = {
            k: v for k, v in (("low_liquidity_hours", low_liquidity_hours), ("stop_buffer_pct", stop_buffer_pct)) if v is not None
        }
        self._demo: dict[int, DemoAdapter] = {}

    def demo_adapter(self, user_id: int, scenario: Optional[str] = None) -> DemoAdapter:
        adapter = self._demo.get(user_id)
        if adapter is None:
            adapter = self._demo[user_id] = DemoAdapter(scenario or "normal", seed=self.demo_seed)
        elif scenario:
            adapter.set_scenario(scenario)
        return adapter

    def drop_demo(self, user_id: int) -> None:
        self._demo.pop(user_id, None)

    def _funding(self) -> dict[tuple[str, str], FundingInfo]:
        return self.market.funding if self.market else {}

    def _demo_funding(self, positions: list[Position]) -> dict[tuple[str, str], FundingInfo]:
        """The demo book uses the live Binance rate of the same symbol, when one has been fetched."""
        live = self._funding()
        out = {}
        for p in positions:
            info = live.get((Venue.BINANCE.value, p.base_symbol))
            if info is not None:
                out[(Venue.DEMO.value, p.base_symbol)] = info
        return out

    def _wicks(self, positions: list[Position]) -> dict[tuple[str, str], WickProfile]:
        return self.wicks.request(positions) if self.wicks else {}

    def _demo_wicks(self, positions: list[Position]) -> dict[tuple[str, str], WickProfile]:
        """Demo book: the live Binance profile of the same symbol when fetched, else the labelled demo profile."""
        live = self.wicks.request_keys([(Venue.BINANCE.value, p.base_symbol) for p in positions]) if self.wicks else {}
        out = {}
        for p in positions:
            profile = live.get((Venue.BINANCE.value, p.base_symbol)) or DEMO_WICKS.get(p.base_symbol)
            if profile is not None:
                out[(Venue.DEMO.value, p.base_symbol)] = profile
        return out

    async def _orders(self, user: User, adapter: ExchangeAdapter, binding: ExchangeBinding) -> Optional[list[OpenOrder]]:
        try:
            return await adapter.get_open_orders()
        except Exception as e:
            logger.warning("User %s: %s/%s open orders unavailable (%s); stop-loss check skipped",
                           user.telegram_id, binding.venue, binding.network, type(e).__name__)
            return None

    async def live_book(self, user: User) -> tuple[list[Position], list[MarginAccount]]:
        positions, accounts, _ = await self._live(user)
        return positions, accounts

    async def _live(self, user: User) -> tuple[list[Position], list[MarginAccount], dict[tuple[str, str], list[OpenOrder]]]:
        positions: list[Position] = []
        accounts: list[MarginAccount] = []
        orders: dict[tuple[str, str], list[OpenOrder]] = {}
        for binding in user.bindings:
            if not binding.is_active:
                continue
            try:
                async with self._adapter_factory(binding) as adapter:
                    try:
                        account = await adapter.get_account()
                    except UnsupportedBinding:
                        raise
                    except Exception as e:
                        logger.warning(
                            "User %s: %s/%s account details unavailable (%s); exchange liquidation prices only",
                            user.telegram_id, binding.venue, binding.network, type(e).__name__,
                        )
                        account = None
                    if account is None:
                        positions.extend(await adapter.get_positions())
                    else:
                        accounts.append(account)
                        positions.extend(account.positions)
                    venue_orders = await self._orders(user, adapter, binding)
                    if venue_orders is not None:
                        orders[(binding.venue, binding.network)] = venue_orders
            except UnsupportedBinding as e:
                logger.warning("User %s: %s", user.telegram_id, e)
            except Exception as e:
                logger.error("User %s: fetching %s/%s failed: %s", user.telegram_id, binding.venue, binding.network, type(e).__name__)
        return positions, accounts, orders

    async def live_positions(self, user: User) -> list[Position]:
        positions, _ = await self.live_book(user)
        return positions

    async def snapshot_for(self, user: User) -> BookSnapshot:
        """Positions, label and risk context for exactly this user. An empty label means nothing is connected."""
        if user.is_demo_mode:
            sim = self.simulations.get(user.telegram_id)
            if sim is not None and sim.state != "idle":
                return BookSnapshot(list(sim.positions), LABEL_SIMULATION, sim.risk_context(self.fees))
            adapter = self.demo_adapter(user.telegram_id)
            positions = await adapter.get_positions()
            return BookSnapshot(positions, LABEL_DEMO, RiskContext(
                funding=self._demo_funding(positions), fees=self.fees,
                orders={(Venue.DEMO.value, "mainnet"): await adapter.get_open_orders()},
                wicks=self._demo_wicks(positions), **self._context_options,
            ))
        if not user.has_any_binding():
            return BookSnapshot([], "")
        positions, accounts, orders = await self._live(user)
        return BookSnapshot(positions, LABEL_LIVE, RiskContext(
            accounts=accounts, funding=self._funding(), fees=self.fees, orders=orders,
            wicks=self._wicks(positions), **self._context_options,
        ))

    async def positions_for(self, user: User) -> tuple[list[Position], str]:
        """(positions, label). An empty label means the user has nothing connected."""
        snapshot = await self.snapshot_for(user)
        return snapshot.positions, snapshot.label
