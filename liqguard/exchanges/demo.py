"""Demo adapter with simulated positions (one instance per user, no shared state)."""

from __future__ import annotations

import copy
import random
from typing import Optional

from liqguard.models import Position, Side, Venue

from .base import ExchangeAdapter, KeyCheck

DEMO_POSITIONS_CONFIG = (
    {"symbol": "BTCUSDT", "side": Side.LONG, "size": 0.5, "entry_price": 65000.0, "leverage": 10, "margin": 3250.0},
    {"symbol": "ETHUSDT", "side": Side.SHORT, "size": 5.0, "entry_price": 3200.0, "leverage": 20, "margin": 800.0},
    {"symbol": "SOLUSDT", "side": Side.LONG, "size": 100.0, "entry_price": 150.0, "leverage": 5, "margin": 3000.0},
)

DEMO_BASE_PRICES = {"BTCUSDT": 65000.0, "ETHUSDT": 3200.0, "SOLUSDT": 150.0}

SCENARIOS = ("normal", "volatile", "critical", "safe", "liquidation_path")

_SCENARIO_VOLATILITY = {"volatile": 0.05, "critical": 0.08, "liquidation_path": 0.03}


class PriceSimulator:
    """Random-walk price simulator. Owns copies of its base prices."""

    def __init__(self, base_prices: dict[str, float], rng: Optional[random.Random] = None):
        self.base_prices = dict(base_prices)
        self.current_prices = dict(base_prices)
        self._rng = rng or random.Random()
        self._step = 0
        self._scenario = "normal"
        self._volatility = 0.02

    def set_scenario(self, scenario: str) -> None:
        self._scenario = scenario
        self._step = 0
        self.current_prices = dict(self.base_prices)
        self._volatility = _SCENARIO_VOLATILITY.get(scenario, 0.02)

    def step(self) -> dict[str, float]:
        self._step += 1
        for symbol in self.current_prices:
            if self._scenario == "liquidation_path":
                drift = -0.005 * (self._step / 10)
            elif self._scenario == "recovery":
                drift = 0.003
            else:
                drift = 0
            change = 1 + drift + self._rng.gauss(0, self._volatility)
            self.current_prices[symbol] = max(self.current_prices[symbol] * change, self.base_prices[symbol] * 0.5)
        return dict(self.current_prices)

    def get_prices(self) -> dict[str, float]:
        return dict(self.current_prices)

    def reset(self) -> None:
        self.current_prices = dict(self.base_prices)
        self._step = 0


class DemoAdapter(ExchangeAdapter):
    """
    Simulated positions for /demo scenarios and screenshot imports.

    All state (positions, prices) is per instance, so one user's imported
    position never shows up in another user's demo book.
    """

    venue = "demo"

    def __init__(self, scenario: str = "normal", seed: Optional[int] = None):
        self.scenario = scenario
        self._rng = random.Random(seed)
        self._positions_config = [copy.copy(p) for p in DEMO_POSITIONS_CONFIG]
        self._simulator = PriceSimulator(DEMO_BASE_PRICES, self._rng)
        self._simulator.set_scenario(scenario)

    @staticmethod
    def _liquidation_price(side: Side, entry_price: float, leverage: float, mmr: float = 0.004) -> float:
        if side == Side.LONG:
            return entry_price * (1 - 1 / leverage + mmr)
        return entry_price * (1 + 1 / leverage - mmr)

    def _mark_for(self, config: dict, current: float) -> float:
        if "mark_price" in config and self.scenario not in ("volatile", "critical"):
            return config["mark_price"]
        long = config["side"] == Side.LONG
        u = self._rng.uniform
        if self.scenario == "volatile":
            return current * (u(0.88, 0.95) if long else u(1.05, 1.12))
        if self.scenario == "critical":
            return current * (u(0.82, 0.88) if long else u(1.12, 1.18))
        if self.scenario in ("safe", "liquidation_path"):
            return current
        return current * (u(0.95, 1.02) if long else u(0.98, 1.05))

    async def get_positions(self) -> list[Position]:
        prices = self._simulator.get_prices()
        positions = []
        for config in self._positions_config:
            side, entry, size = config["side"], config["entry_price"], config["size"]
            mark = self._mark_for(config, prices.get(config["symbol"], entry))
            direction = 1 if side == Side.LONG else -1
            positions.append(Position(
                venue=Venue.DEMO,
                symbol=config["symbol"],
                side=side,
                size=size,
                entry_price=entry,
                mark_price=mark,
                liquidation_price=config.get("liquidation_price") or self._liquidation_price(side, entry, config["leverage"]),
                margin=config["margin"],
                leverage=config["leverage"],
                unrealized_pnl=(mark - entry) * size * direction,
            ))
        return positions

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        return self._simulator.get_prices().get(symbol.replace("-PERP", ""))

    async def check_key(self) -> KeyCheck:
        return KeyCheck(True, "Demo mode - no credentials required", ["demo"], True)

    def set_scenario(self, scenario: str) -> None:
        self.scenario = scenario
        self._simulator.set_scenario(scenario)

    def step_simulation(self) -> dict[str, float]:
        return self._simulator.step()

    def add_custom_position(
        self,
        symbol: str,
        side: Side,
        size: float,
        entry_price: float,
        mark_price: float,
        leverage: float,
        margin: float,
        liquidation_price: Optional[float] = None,
    ) -> None:
        """Add a position (e.g. from a parsed screenshot) to this user's demo book only."""
        self._positions_config.append({
            "symbol": symbol,
            "side": side,
            "size": size,
            "entry_price": entry_price,
            "mark_price": mark_price,
            "leverage": leverage,
            "margin": margin,
            "liquidation_price": liquidation_price,
        })
        self._simulator.base_prices[symbol] = entry_price
        self._simulator.current_prices[symbol] = mark_price

    def clear_positions(self) -> None:
        self._positions_config = []
