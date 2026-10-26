"""Demo connector with simulated positions and price paths for testing."""

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Callable, Optional

from liqguard.models import Position, Side, Venue

from .base import BaseConnector

logger = logging.getLogger(__name__)


DEMO_POSITIONS_CONFIG = [
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


DEMO_BASE_PRICES = {
    "BTCUSDT": 65000.0,
    "ETHUSDT": 3200.0,
    "SOLUSDT": 150.0,
}


class PriceSimulator:
    """
    Simulates price paths for demo mode.
    
    Supports various scenarios including gradual movement toward liquidation.
    """
    
    def __init__(self, base_prices: dict[str, float]):
        self.base_prices = base_prices.copy()
        self.current_prices = base_prices.copy()
        self._step = 0
        self._scenario = "normal"
        self._volatility = 0.02
    
    def set_scenario(self, scenario: str) -> None:
        """Set simulation scenario."""
        self._scenario = scenario
        self._step = 0
        self.current_prices = self.base_prices.copy()
        
        if scenario == "volatile":
            self._volatility = 0.05
        elif scenario == "critical":
            self._volatility = 0.08
        elif scenario == "liquidation_path":
            self._volatility = 0.03
        else:
            self._volatility = 0.02
    
    def step(self) -> dict[str, float]:
        """
        Advance simulation by one step and return new prices.
        """
        self._step += 1
        
        for symbol in self.current_prices:
            if self._scenario == "liquidation_path":
                drift = -0.005 * (self._step / 10)
            elif self._scenario == "recovery":
                drift = 0.003
            else:
                drift = 0
            
            noise = random.gauss(0, self._volatility)
            change = 1 + drift + noise
            
            self.current_prices[symbol] = max(
                self.current_prices[symbol] * change,
                self.base_prices[symbol] * 0.5
            )
        
        return self.current_prices.copy()
    
    def get_prices(self) -> dict[str, float]:
        """Get current prices without stepping."""
        return self.current_prices.copy()
    
    def reset(self) -> None:
        """Reset to base prices."""
        self.current_prices = self.base_prices.copy()
        self._step = 0


class DemoConnector(BaseConnector):
    """
    Demo connector that simulates exchange positions.
    
    Features:
    - Multiple scenarios (normal, volatile, critical, safe)
    - Simulated price paths toward liquidation
    - Callback support for alert testing
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
                - "liquidation_path": Gradual movement toward liquidation
        """
        self.scenario = scenario
        self._positions_config = [p.copy() for p in DEMO_POSITIONS_CONFIG]
        self._simulator = PriceSimulator(DEMO_BASE_PRICES)
        self._simulator.set_scenario(scenario)
        
        self._alert_callback: Optional[Callable] = None
        self._simulation_task: Optional[asyncio.Task] = None
        self._running = False
    
    @property
    def venue_name(self) -> str:
        return "demo"
    
    def _calculate_liquidation_price(
        self,
        side: Side,
        entry_price: float,
        leverage: float,
        maintenance_margin_rate: float = 0.004
    ) -> float:
        """Calculate approximate liquidation price."""
        if side == Side.LONG:
            return entry_price * (1 - 1/leverage + maintenance_margin_rate)
        else:
            return entry_price * (1 + 1/leverage - maintenance_margin_rate)
    
    def _generate_positions(self) -> list[dict]:
        """Generate positions based on scenario and current prices."""
        positions = []
        prices = self._simulator.get_prices()
        
        for config in self._positions_config:
            pos = config.copy()
            symbol = pos["symbol"]
            
            base_price = DEMO_BASE_PRICES[symbol]
            current_price = prices.get(symbol, base_price)
            
            if self.scenario == "volatile":
                if pos["side"] == Side.LONG:
                    pos["mark_price"] = current_price * random.uniform(0.88, 0.95)
                else:
                    pos["mark_price"] = current_price * random.uniform(1.05, 1.12)
            elif self.scenario == "critical":
                if pos["side"] == Side.LONG:
                    pos["mark_price"] = current_price * random.uniform(0.82, 0.88)
                else:
                    pos["mark_price"] = current_price * random.uniform(1.12, 1.18)
            elif self.scenario == "safe":
                pos["mark_price"] = current_price
            elif self.scenario == "liquidation_path":
                pos["mark_price"] = current_price
            else:
                if pos["side"] == Side.LONG:
                    pos["mark_price"] = current_price * random.uniform(0.95, 1.02)
                else:
                    pos["mark_price"] = current_price * random.uniform(0.98, 1.05)
            
            positions.append(pos)
        
        return positions
    
    async def get_positions(self) -> list[Position]:
        """Get simulated positions."""
        positions = []
        generated = self._generate_positions()
        
        for pos_data in generated:
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
        prices = self._simulator.get_prices()
        
        clean_symbol = symbol.replace("-PERP", "")
        if clean_symbol in prices:
            return prices[clean_symbol]
        
        return DEMO_BASE_PRICES.get(clean_symbol)
    
    async def validate_credentials(self) -> tuple[bool, str, list[str]]:
        """Demo mode always validates successfully."""
        return True, "Demo mode - no credentials required", ["demo"]
    
    def set_scenario(self, scenario: str) -> None:
        """Change the demo scenario."""
        self.scenario = scenario
        self._simulator.set_scenario(scenario)
    
    def step_simulation(self) -> dict[str, float]:
        """Advance the simulation by one step."""
        return self._simulator.step()
    
    def reset_simulation(self) -> None:
        """Reset simulation to initial state."""
        self._simulator.reset()
    
    def set_alert_callback(self, callback: Callable) -> None:
        """Set callback for alert notifications during simulation."""
        self._alert_callback = callback
    
    async def start_liquidation_simulation(
        self,
        interval_seconds: float = 2.0,
        on_alert: Optional[Callable] = None,
    ) -> None:
        """
        Start a simulation that gradually moves positions toward liquidation.
        
        Fires alerts at warning (20%), critical (10%), and emergency (5%) levels.
        """
        self.set_scenario("liquidation_path")
        self._running = True
        
        alert_levels = {
            "warning": 20.0,
            "critical": 10.0,
            "emergency": 5.0,
        }
        fired_alerts: set[str] = set()
        
        callback = on_alert or self._alert_callback
        
        while self._running:
            self._simulator.step()
            positions = await self.get_positions()
            
            for pos in positions:
                distance = pos.distance_to_liquidation_pct
                
                for level, threshold in alert_levels.items():
                    alert_key = f"{pos.symbol}_{level}"
                    if distance <= threshold and alert_key not in fired_alerts:
                        fired_alerts.add(alert_key)
                        
                        if callback:
                            try:
                                await callback(level, pos, distance)
                            except Exception as e:
                                logger.error(f"Alert callback error: {e}")
                        
                        logger.info(f"DEMO ALERT: {level.upper()} - {pos.symbol} at {distance:.1f}%")
                        
                        if distance <= 0:
                            logger.info(f"DEMO: {pos.symbol} would be liquidated!")
                            self._running = False
            
            await asyncio.sleep(interval_seconds)
    
    def stop_simulation(self) -> None:
        """Stop the running simulation."""
        self._running = False
    
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
        self._positions_config.append({
            "symbol": symbol,
            "side": side,
            "size": size,
            "entry_price": entry_price,
            "mark_price": mark_price,
            "leverage": leverage,
            "margin": margin,
        })
        
        DEMO_BASE_PRICES[symbol] = entry_price
        self._simulator.base_prices[symbol] = entry_price
        self._simulator.current_prices[symbol] = mark_price
    
    def clear_positions(self) -> None:
        """Clear all demo positions."""
        self._positions_config = []
    
    async def close(self) -> None:
        """Stop any running simulation and cleanup."""
        self.stop_simulation()
