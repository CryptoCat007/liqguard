"""Taker fee schedule (configurable per venue)."""

from __future__ import annotations

from dataclasses import dataclass, field

from liqguard.models import Venue

# Base-tier taker fees; override with LIQGUARD_TAKER_FEE_BINANCE / LIQGUARD_TAKER_FEE_HYPERLIQUID.
DEFAULT_TAKER_FEES = {"binance": 0.0005, "hyperliquid": 0.00045, "demo": 0.0005}


@dataclass(frozen=True)
class FeeSchedule:
    taker: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TAKER_FEES))

    def taker_rate(self, venue: Venue | str) -> float:
        key = venue.value if isinstance(venue, Venue) else venue
        return self.taker.get(key, DEFAULT_TAKER_FEES["binance"])

    def taker_fee(self, venue: Venue | str, notional: float) -> float:
        return abs(notional) * self.taker_rate(venue)

    def to_dict(self) -> dict:
        return {"taker": dict(self.taker)}
