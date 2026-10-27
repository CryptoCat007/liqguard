"""Common interface every exchange adapter implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from liqguard.models import MAINNET, Position


class ReadOnlyViolation(RuntimeError):
    """Raised when code tries to send a state-changing request with a read-only adapter."""


@dataclass(frozen=True)
class KeyCheck:
    """Outcome of validating credentials against LiqGuard's key policy."""

    accepted: bool
    message: str
    permissions: list[str] = field(default_factory=list)
    verified: bool = False

    def __bool__(self) -> bool:
        return self.accepted


class ExchangeAdapter(ABC):
    """
    Read positions and mark prices from one venue for one user's credentials.

    Adapters hold exactly one user's credentials and must never be shared
    between users. Use as an async context manager so HTTP clients close.
    """

    venue: str = ""
    network: str = MAINNET

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Open positions, normalized."""

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """Current mark price, or None if unknown."""

    @abstractmethod
    async def check_key(self) -> KeyCheck:
        """Validate credentials and enforce the key policy for this network."""

    async def close(self) -> None:
        """Release network resources."""

    async def __aenter__(self) -> "ExchangeAdapter":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
