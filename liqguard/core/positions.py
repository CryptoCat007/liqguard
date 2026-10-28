"""Where a user's positions come from: their simulation, their demo book, or their own exchange bindings."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from liqguard.exchanges import DemoAdapter, ExchangeAdapter, UnsupportedBinding, adapter_for_binding
from liqguard.models import ExchangeBinding, FundingInfo, MarginAccount, Position, User, Venue
from liqguard.risk import FeeSchedule, RiskContext
from liqguard.simulation import SimulationRegistry

from .market import MarketDataService

logger = logging.getLogger(__name__)

AdapterFactory = Callable[[ExchangeBinding], ExchangeAdapter]

LABEL_SIMULATION = "📊 Demo Mode · market simulation"
LABEL_DEMO = "📊 Demo Mode"
LABEL_LIVE = "📊 Live Positions"


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
    ):
        self.simulations = simulations
        self._adapter_factory = adapter_factory
        self.market = market
        self.fees = fees or FeeSchedule()
        self._demo: dict[int, DemoAdapter] = {}

    def demo_adapter(self, user_id: int, scenario: Optional[str] = None) -> DemoAdapter:
        adapter = self._demo.get(user_id)
        if adapter is None:
            adapter = self._demo[user_id] = DemoAdapter(scenario or "normal")
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

    async def live_book(self, user: User) -> tuple[list[Position], list[MarginAccount]]:
        positions: list[Position] = []
        accounts: list[MarginAccount] = []
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
            except UnsupportedBinding as e:
                logger.warning("User %s: %s", user.telegram_id, e)
            except Exception as e:
                logger.error("User %s: fetching %s/%s failed: %s", user.telegram_id, binding.venue, binding.network, type(e).__name__)
        return positions, accounts

    async def live_positions(self, user: User) -> list[Position]:
        positions, _ = await self.live_book(user)
        return positions

    async def snapshot_for(self, user: User) -> BookSnapshot:
        """Positions, label and risk context for exactly this user. An empty label means nothing is connected."""
        if user.is_demo_mode:
            sim = self.simulations.get(user.telegram_id)
            if sim is not None and sim.state != "idle":
                return BookSnapshot(list(sim.positions), LABEL_SIMULATION, sim.risk_context(self.fees))
            positions = await self.demo_adapter(user.telegram_id).get_positions()
            return BookSnapshot(positions, LABEL_DEMO, RiskContext(funding=self._demo_funding(positions), fees=self.fees))
        if not user.has_any_binding():
            return BookSnapshot([], "")
        positions, accounts = await self.live_book(user)
        return BookSnapshot(positions, LABEL_LIVE, RiskContext(accounts=accounts, funding=self._funding(), fees=self.fees))

    async def positions_for(self, user: User) -> tuple[list[Position], str]:
        """(positions, label). An empty label means the user has nothing connected."""
        snapshot = await self.snapshot_for(user)
        return snapshot.positions, snapshot.label
