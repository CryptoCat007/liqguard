"""Where a user's positions come from: their simulation, their demo book, or their own exchange bindings."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from liqguard.exchanges import DemoAdapter, ExchangeAdapter, UnsupportedBinding, adapter_for_binding
from liqguard.models import ExchangeBinding, Position, User
from liqguard.simulation import SimulationRegistry

logger = logging.getLogger(__name__)

AdapterFactory = Callable[[ExchangeBinding], ExchangeAdapter]

LABEL_SIMULATION = "📊 Demo Mode · market simulation"
LABEL_DEMO = "📊 Demo Mode"
LABEL_LIVE = "📊 Live Positions"


class PositionService:
    """
    Resolves positions for exactly one user at a time.

    Demo books are per user; live positions are fetched with adapters built
    from that user's own decrypted bindings and closed right after use.
    """

    def __init__(self, simulations: SimulationRegistry, adapter_factory: AdapterFactory = adapter_for_binding):
        self.simulations = simulations
        self._adapter_factory = adapter_factory
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

    async def live_positions(self, user: User) -> list[Position]:
        positions: list[Position] = []
        for binding in user.bindings:
            if not binding.is_active:
                continue
            try:
                async with self._adapter_factory(binding) as adapter:
                    positions.extend(await adapter.get_positions())
            except UnsupportedBinding as e:
                logger.warning("User %s: %s", user.telegram_id, e)
            except Exception as e:
                logger.error("User %s: fetching %s/%s failed: %s", user.telegram_id, binding.venue, binding.network, type(e).__name__)
        return positions

    async def positions_for(self, user: User) -> tuple[list[Position], str]:
        """(positions, label). An empty label means the user has nothing connected."""
        if user.is_demo_mode:
            sim = self.simulations.get(user.telegram_id)
            if sim is not None and sim.state != "idle":
                return list(sim.positions), LABEL_SIMULATION
            return await self.demo_adapter(user.telegram_id).get_positions(), LABEL_DEMO
        if not user.has_any_binding():
            return [], ""
        return await self.live_positions(user), LABEL_LIVE

    async def monitored_positions(self, user: User) -> list[Position]:
        positions, _ = await self.positions_for(user)
        return positions
