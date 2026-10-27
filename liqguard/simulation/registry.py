"""One simulation per owner, plus a public demo simulation."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from .market import MarketSimulation

logger = logging.getLogger(__name__)

SimulationFactory = Callable[[Optional[int]], MarketSimulation]
Cleanup = Callable[[], None]
CreateHook = Callable[[MarketSimulation], Optional[Cleanup]]


class SimulationRegistry:
    """
    Hands out the simulation that belongs to a user.

    `public` (owner None) backs the unauthenticated demo page. Per-user
    simulations are created lazily; `on_create` hooks attach owner-scoped
    delivery (Telegram bridge, audit mirroring) to each new user simulation.
    """

    def __init__(self, factory: SimulationFactory, max_user_simulations: int = 1000):
        self._factory = factory
        self._max = max_user_simulations
        self._sims: dict[int, MarketSimulation] = {}
        self._hooks: list[CreateHook] = []
        self._cleanups: dict[int, list[Cleanup]] = {}
        self.public = factory(None)

    def on_create(self, hook: CreateHook) -> None:
        self._hooks.append(hook)
        for sim in self._sims.values():
            self._attach(sim, hook)

    def _attach(self, sim: MarketSimulation, hook: CreateHook) -> None:
        cleanup = hook(sim)
        if cleanup is not None:
            self._cleanups.setdefault(sim.owner_id, []).append(cleanup)

    def get(self, owner_id: int) -> Optional[MarketSimulation]:
        return self._sims.get(owner_id)

    def get_or_create(self, owner_id: int) -> MarketSimulation:
        if owner_id is None:
            raise ValueError("owner_id is required; use .public for the demo page")
        sim = self._sims.get(owner_id)
        if sim is None:
            self._evict_if_full()
            sim = self._factory(owner_id)
            if sim.owner_id != owner_id:
                raise ValueError("factory returned a simulation for a different owner")
            self._sims[owner_id] = sim
            for hook in self._hooks:
                self._attach(sim, hook)
        return sim

    def _evict_if_full(self) -> None:
        if len(self._sims) < self._max:
            return
        for owner_id, sim in list(self._sims.items()):
            if not sim.is_running:
                self.discard(owner_id)
                return
        raise RuntimeError("Too many concurrent simulations")

    def discard(self, owner_id: int) -> None:
        self._sims.pop(owner_id, None)
        for cleanup in self._cleanups.pop(owner_id, []):
            cleanup()

    def __len__(self) -> int:
        return len(self._sims)

    async def stop_all(self) -> None:
        sims = [self.public, *self._sims.values()]
        await asyncio.gather(*(s.stop(source="shutdown") for s in sims), return_exceptions=True)
