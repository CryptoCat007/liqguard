"""Base connector interface for exchange APIs."""

from abc import ABC, abstractmethod
from typing import Optional

from liqguard.models import Position


class BaseConnector(ABC):
    """Abstract base class for exchange connectors."""

    @property
    @abstractmethod
    def venue_name(self) -> str:
        """Return the venue name."""
        pass

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """
        Fetch all open positions from the exchange.

        Returns:
            List of normalized Position objects.
        """
        pass

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """
        Get current mark price for a symbol.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")

        Returns:
            Mark price or None if not found.
        """
        pass

    @abstractmethod
    async def validate_credentials(self) -> tuple[bool, str, list[str]]:
        """
        Validate the provided credentials.

        Returns:
            Tuple of (is_valid, message, permissions_list)
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close any open connections."""
        pass
