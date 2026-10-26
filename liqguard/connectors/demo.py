"""Demo connector with simulated positions for testing and demos."""

import logging
import random
from typing import Optional

from liqguard.models import Position, Side, Venue

from .base import BaseConnector

logger = logging.getLogger(__name__)


DEMO_POSITIONS = [
    {
        "symbol": "BTCUSDT",
        "side": Side.LONG,
        "size": 0.5,
        "entry_price": 65000.0,
        "leverage": 10,
        "margin": 3250.0,
    },
    {
        "symbol": "ETHUSDT",
        "side": Side.SHORT,
        "size": 5.0,
        "entry_price": 3200.0,
        "leverage": 20,
        "margin": 800.0,
    },
    {
        "symbol": "SOLUSDT",
        "side": Side.LONG,
        "size": 100.0,
        "entry_price": 150.0,
        "leverage": 5,
        "margin": 3000.0,
    },
]


DEMO_MARK_PRICES = {
    "BTCUSDT": 64500.0,
    "ETHUSDT": 3250.0,
    "SOLUSDT": 148.0,
}


class DemoConnector(BaseConnector):
    """
    Demo connector that simulates exchange positions.

    Used for testing and demo purposes without real money or API keys.
    """

    def __init__(self, scenario: str = "normal"):
        """
        Initialize demo connector.

        Args:
            scenario: Demo scenario type
                - "normal": Normal market conditions
                - "volatile": High volatility, positions near liquidation
                - "critical": Positions very close to liquidation
                - "safe": All positions are safe
        """
        self.scenario = scenario
        self._positions = self._generate_positions()

    @property
    def venue_name(self) -> str:
        return "demo"

    def _generate_positions(self) -> list[dict]:
        """Generate positions based on scenario."""
        positions = []

        for pos_template in DEMO_POSITIONS:
            pos = pos_template.copy()

            base_mark = DEMO_MARK_PRICES[pos["symbol"]]

            if self.scenario == "volatile":
                if pos["side"] == Side.LONG:
                    pos["mark_price"] = base_mark * random.uniform(0.88, 0.95)
                else:
                    pos["mark_price"] = base_mark * random.uniform(1.05, 1.12)
            elif self.scenario == "critical":
                if pos["side"] == Side.LONG:
                    pos["mark_price"] = base_mark * random.uniform(0.82, 0.88)
                else:
                    pos["mark_price"] = base_mark * random.uniform(1.12, 1.18)
            elif self.scenario == "safe":
                pos["mark_price"] = base_mark
            else:
                if pos["side"] == Side.LONG:
                    pos["mark_price"] = base_mark * random.uniform(0.95, 1.02)
                else:
                    pos["mark_price"] = base_mark * random.uniform(0.98, 1.05)

            positions.append(pos)

        return positions

    def _calculate_liquidation_price(
        self,
        side: Side,
        entry_price: float,
        leverage: float,
        maintenance_margin_rate: float = 0.004
    ) -> float:
        """
        Calculate approximate liquidation price.

        Simplified formula:
        - Long: entry * (1 - 1/leverage + maintenance_rate)
        - Short: entry * (1 + 1/leverage - maintenance_rate)
        """
        if side == Side.LONG:
            return entry_price * (1 - 1/leverage + maintenance_margin_rate)
        else:
            return entry_price * (1 + 1/leverage - maintenance_margin_rate)

    async def get_positions(self) -> list[Position]:
        """Get simulated positions."""
        positions = []

        for pos_data in self._positions:
            side = pos_data["side"]
            entry_price = pos_data["entry_price"]
            mark_price = pos_data.get("mark_price", entry_price)
            leverage = pos_data["leverage"]
            size = pos_data["size"]
            margin = pos_data["margin"]

            liquidation_price = self._calculate_liquidation_price(
                side, entry_price, leverage
            )

            if side == Side.LONG:
                unrealized_pnl = (mark_price - entry_price) * size
            else:
                unrealized_pnl = (entry_price - mark_price) * size

            position = Position(
                venue=Venue.DEMO,
                symbol=pos_data["symbol"],
                side=side,
                size=size,
                entry_price=entry_price,
                mark_price=mark_price,
                liquidation_price=liquidation_price,
                margin=margin,
                leverage=leverage,
                unrealized_pnl=unrealized_pnl,
                margin_ratio=None,
            )
            positions.append(position)

        return positions

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """Get simulated mark price."""
        for pos in self._positions:
            if pos["symbol"] == symbol:
                return pos.get("mark_price")

        return DEMO_MARK_PRICES.get(symbol)

    async def validate_credentials(self) -> tuple[bool, str, list[str]]:
        """Demo mode always validates successfully."""
        return True, "Demo mode - no credentials required", ["demo"]

    def set_scenario(self, scenario: str) -> None:
        """Change the demo scenario and regenerate positions."""
        self.scenario = scenario
        self._positions = self._generate_positions()

    def add_custom_position(
        self,
        symbol: str,
        side: Side,
        size: float,
        entry_price: float,
        mark_price: float,
        leverage: float,
        margin: float,
    ) -> None:
        """Add a custom position for demo purposes."""
        self._positions.append({
            "symbol": symbol,
            "side": side,
            "size": size,
            "entry_price": entry_price,
            "mark_price": mark_price,
            "leverage": leverage,
            "margin": margin,
        })

    def clear_positions(self) -> None:
        """Clear all demo positions."""
        self._positions = []

    async def close(self) -> None:
        """No cleanup needed for demo connector."""
        pass
