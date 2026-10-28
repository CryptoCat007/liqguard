"""Parse open orders from each venue into `OpenOrder` (read-only)."""

from __future__ import annotations

from typing import Optional

from liqguard.models import OpenOrder, Side, Venue
from liqguard.models.orders import LIMIT, OTHER, STOP_LIMIT, STOP_MARKET, TAKE_PROFIT, TRAILING_STOP

BINANCE_TYPES = {
    "STOP_MARKET": STOP_MARKET,
    "STOP": STOP_LIMIT,
    "TRAILING_STOP_MARKET": TRAILING_STOP,
    "TAKE_PROFIT_MARKET": TAKE_PROFIT,
    "TAKE_PROFIT": TAKE_PROFIT,
    "LIMIT": LIMIT,
    "MARKET": OTHER,
}

HYPERLIQUID_TYPES = {
    "stop market": STOP_MARKET,
    "stop limit": STOP_LIMIT,
    "take profit market": TAKE_PROFIT,
    "take profit limit": TAKE_PROFIT,
    "limit": LIMIT,
}


def _flag(value) -> bool:
    return value is True or str(value).lower() == "true"


def _price(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_binance_order(row: dict) -> Optional[OpenOrder]:
    """Regular (`/fapi/v1/openOrders`) and conditional algo orders (`/fapi/v1/openAlgoOrders`) share this parser."""
    try:
        raw_type = str(row.get("type") or row.get("orderType") or row.get("origType") or "").upper()
        side = Side.LONG if str(row["side"]).upper() == "BUY" else Side.SHORT
        order_type = BINANCE_TYPES.get(raw_type, OTHER)
        trigger = _price(row.get("stopPrice")) or _price(row.get("triggerPrice"))
        if order_type == TRAILING_STOP:
            trigger = None
        return OpenOrder(
            venue=Venue.BINANCE,
            symbol=row["symbol"],
            side=side,
            order_type=order_type,
            trigger_price=trigger,
            limit_price=_price(row.get("price")),
            quantity=_price(row.get("origQty")) or _price(row.get("quantity")),
            reduce_only=_flag(row.get("reduceOnly")),
            close_position=_flag(row.get("closePosition")),
            position_side=(row.get("positionSide") or None),
            order_id=str(row.get("orderId") or row.get("algoId") or "") or None,
        )
    except (KeyError, TypeError):
        return None


def parse_hyperliquid_order(row: dict) -> Optional[OpenOrder]:
    """`frontendOpenOrders` row. Position TP/SL orders (and size 0) close the whole position."""
    try:
        coin = row["coin"]
        side = Side.LONG if row["side"] == "B" else Side.SHORT
        order_type = HYPERLIQUID_TYPES.get(str(row.get("orderType", "")).lower(), OTHER)
        size = _price(row.get("sz")) or _price(row.get("origSz"))
        whole = bool(row.get("isPositionTpsl")) or size is None
        return OpenOrder(
            venue=Venue.HYPERLIQUID,
            symbol=f"{coin}-PERP",
            side=side,
            order_type=order_type,
            trigger_price=_price(row.get("triggerPx")) if row.get("isTrigger") else None,
            limit_price=_price(row.get("limitPx")),
            quantity=size,
            reduce_only=_flag(row.get("reduceOnly")) or whole,
            close_position=whole,
            order_id=str(row.get("oid")) if row.get("oid") is not None else None,
        )
    except (KeyError, TypeError):
        return None
