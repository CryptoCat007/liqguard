"""
Auto-deleveraging (ADL) indicator.

Binance publishes each position's ADL quantile (0 … 4) via GET
/fapi/v1/adlQuantile; positionRisk rows that carry `adlQuantile`/`adl` are read
directly. LiqGuard shows it as 1 … 5 lights like the Binance app. ADL closes *profitable*, highly leveraged positions
against bankrupt ones when the insurance fund can't absorb a liquidation, so
the warning fires only at 4–5 lights on a position in profit.

Hyperliquid does not publish a per-position ADL rank, so it is reported as
unsupported rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from liqguard.models import Position, Venue

ADL_WARN_LIGHTS = 4
MAX_LIGHTS = 5


def lights_from_quantile(quantile: Optional[int]) -> Optional[int]:
    if quantile is None:
        return None
    return max(1, min(MAX_LIGHTS, int(quantile) + 1))


@dataclass(frozen=True)
class AdlStatus:
    supported: bool
    lights: Optional[int]
    quantile: Optional[int]
    profitable: bool
    warn: bool
    note: str

    def to_dict(self) -> dict:
        return {
            "supported": self.supported,
            "lights": self.lights,
            "quantile": self.quantile,
            "max_lights": MAX_LIGHTS,
            "profitable": self.profitable,
            "warn": self.warn,
            "note": self.note,
        }


def adl_status(position: Position) -> AdlStatus:
    profitable = position.unrealized_pnl > 0
    if position.venue == Venue.HYPERLIQUID:
        return AdlStatus(False, None, None, profitable, False, "Hyperliquid does not publish a per-position ADL rank.")
    lights = lights_from_quantile(position.adl_quantile)
    if lights is None:
        return AdlStatus(True, None, None, profitable, False, "ADL rank not reported yet.")
    warn = lights >= ADL_WARN_LIGHTS and profitable
    if warn:
        note = (f"{lights}/5 ADL lights on a profitable position: if a counterparty is liquidated beyond the "
                f"insurance fund, this position can be closed automatically.")
    elif lights >= ADL_WARN_LIGHTS:
        note = f"{lights}/5 ADL lights, but the position is not in profit, so it is not first in line."
    else:
        note = f"{lights}/5 ADL lights."
    return AdlStatus(True, lights, position.adl_quantile, profitable, warn, note)
