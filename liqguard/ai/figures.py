"""
Numbers as a reader writes them, for Muse's prompts.

Muse copies figures from its input. Raw floats (`5.1534`, `-7674.0`) come
back verbatim, so every prompt passes these strings instead: `5.2%`,
`-$7,674`, `$1,381.22`.
"""

from __future__ import annotations

from typing import Optional


def usd(x: Optional[float], cents: Optional[bool] = None) -> Optional[str]:
    """`$1,381.22`, `-$7,674`; cents are dropped from $1,000 up unless `cents` says otherwise."""
    if x is None:
        return None
    show_cents = abs(x) < 1000 if cents is None else cents
    body = f"${abs(x):,.2f}" if show_cents else f"${abs(x):,.0f}"
    return ("-" if x < 0 and round(abs(x), 2 if show_cents else 0) else "") + body


def signed_usd(x: Optional[float]) -> Optional[str]:
    """`+$1,204` / `-$7,674`."""
    if x is None:
        return None
    return ("+" if x > 0 else "") + usd(x)


def price(x: Optional[float]) -> Optional[str]:
    """A market price at the precision traders quote: `$97,250.50`, `$1.2345`, `$0.000123`."""
    if x is None:
        return None
    if abs(x) >= 100:
        return f"${x:,.2f}"
    if abs(x) >= 1:
        return f"${x:,.4f}"
    return f"${x:.6g}"


def pct(x: Optional[float], dp: int = 1) -> Optional[str]:
    """`5.2%`."""
    return None if x is None else f"{x:.{dp}f}%"
