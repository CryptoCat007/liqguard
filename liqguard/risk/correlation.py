"""
One-factor market model for correlated moves.

Crypto moves with BTC: in a fast move, alts follow BTC with a beta (SOL and
DOGE move further than BTC, BNB less). `beta` is the expected move of a coin
when "the market" (BTC) moves 1%; `rho` is its correlation with the market,
and the pairwise correlation of two coins is `rho_i * rho_j`. Stock perps
mostly follow their own market.

These are conservative static estimates (30–90 day daily-return figures for
majors, rounded); unknown crypto symbols get beta 1.2 / rho 0.7.
"""

from __future__ import annotations

from dataclasses import dataclass

from .sessions import is_stock_perp, underlying

FACTORS: dict[str, tuple[float, float]] = {
    "BTC": (1.00, 1.00),
    "ETH": (1.15, 0.85),
    "SOL": (1.35, 0.78),
    "BNB": (0.80, 0.70),
    "XRP": (1.20, 0.68),
    "DOGE": (1.45, 0.72),
    "AVAX": (1.40, 0.75),
    "LINK": (1.30, 0.74),
    "ADA": (1.25, 0.72),
    "SUI": (1.50, 0.70),
}
DEFAULT_CRYPTO = (1.20, 0.70)
DEFAULT_STOCK = (0.30, 0.25)
CORRELATED = 0.6


@dataclass(frozen=True)
class Factor:
    beta: float
    rho: float


def factor(symbol: str) -> Factor:
    base = underlying(symbol).upper()
    if base in FACTORS:
        return Factor(*FACTORS[base])
    return Factor(*(DEFAULT_STOCK if is_stock_perp(symbol) else DEFAULT_CRYPTO))


def correlation(a: str, b: str) -> float:
    if underlying(a).upper() == underlying(b).upper():
        return 1.0
    return factor(a).rho * factor(b).rho
