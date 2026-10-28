"""Hyperliquid adapter using the public info API (wallet address only, no key)."""

import logging
from typing import Optional

import httpx

from liqguard.models import (
    CROSS,
    ISOLATED,
    MaintenanceBracket,
    MarginAccount,
    Position,
    Side,
    Venue,
)

from .base import ExchangeAdapter, KeyCheck

logger = logging.getLogger(__name__)

HYPERLIQUID_API_URL = "https://api.hyperliquid.xyz/info"


def hyperliquid_maintenance(max_leverage: float) -> tuple[MaintenanceBracket, ...]:
    """Maintenance margin = notional / (2 × max leverage)."""
    return (MaintenanceBracket(0.0, float("inf"), 1 / (2 * max_leverage), 0.0),)


class HyperliquidAdapter(ExchangeAdapter):
    """
    Adapter for Hyperliquid DEX.

    Uses public API - only requires wallet address, no private key needed.
    Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
    """

    def __init__(self, wallet_address: str):
        """
        Initialize Hyperliquid adapter.

        Args:
            wallet_address: User's wallet address (0x...)
        """
        self.wallet_address = wallet_address.lower()
        self.client = httpx.AsyncClient(timeout=30.0)
        self._meta_cache: Optional[dict] = None

    venue = "hyperliquid"

    def __repr__(self) -> str:
        return f"HyperliquidAdapter(wallet={self.wallet_address[:6]}…{self.wallet_address[-4:]})"

    async def _get_meta(self) -> dict:
        """Get exchange metadata (asset info)."""
        if self._meta_cache is not None:
            return self._meta_cache

        response = await self.client.post(
            HYPERLIQUID_API_URL,
            json={"type": "meta"}
        )
        response.raise_for_status()
        self._meta_cache = response.json()
        return self._meta_cache

    async def _clearinghouse(self) -> dict:
        response = await self.client.post(HYPERLIQUID_API_URL, json={"type": "clearinghouseState", "user": self.wallet_address})
        response.raise_for_status()
        return response.json()

    async def _mids(self) -> dict:
        response = await self.client.post(HYPERLIQUID_API_URL, json={"type": "allMids"})
        response.raise_for_status()
        return response.json()

    def _parse_positions(self, state: dict, mids: dict) -> list[Position]:
        positions = []
        for entry in state.get("assetPositions", []):
            pos = entry.get("position", {})
            size = float(pos.get("szi", 0) or 0)
            if size == 0:
                continue
            coin = pos.get("coin", "")
            if coin not in mids:
                logger.warning("No mark price for %s", coin)
                continue
            lev = pos.get("leverage", {})
            lev_value = float(lev.get("value", 1)) if isinstance(lev, dict) else float(lev or 1)
            isolated = isinstance(lev, dict) and lev.get("type") == "isolated"
            margin = float(pos.get("marginUsed", 0) or 0)
            upnl = float(pos.get("unrealizedPnl", 0) or 0)
            positions.append(Position(
                venue=Venue.HYPERLIQUID,
                symbol=f"{coin}-PERP",
                side=Side.LONG if size > 0 else Side.SHORT,
                size=abs(size),
                entry_price=float(pos.get("entryPx", 0) or 0),
                mark_price=float(mids[coin]),
                liquidation_price=float(pos["liquidationPx"]) if pos.get("liquidationPx") else 0.0,
                margin=margin,
                leverage=lev_value,
                unrealized_pnl=upnl,
                margin_ratio=(abs(upnl) / margin * 100 if upnl < 0 else 0) if margin > 0 else None,
                margin_mode=ISOLATED if isolated else CROSS,
                isolated_wallet=margin - upnl if isolated else None,
            ))
        return positions

    async def get_positions(self) -> list[Position]:
        """Fetch all open positions for the wallet address."""
        try:
            return self._parse_positions(await self._clearinghouse(), await self._mids())
        except httpx.HTTPError as e:
            logger.error("HTTP error fetching Hyperliquid positions: %s", type(e).__name__)
            raise

    async def get_account(self) -> MarginAccount:
        """Positions, cross wallet (account value minus cross unrealized PnL) and per-asset maintenance."""
        state = await self._clearinghouse()
        positions = self._parse_positions(state, await self._mids())
        account_value = float(state.get("crossMarginSummary", {}).get("accountValue", 0) or 0)
        wallet = account_value - sum(p.unrealized_pnl for p in positions if p.margin_mode == CROSS)
        schedules = {}
        try:
            meta = await self._get_meta()
            for asset in meta.get("universe", []):
                if asset.get("maxLeverage"):
                    schedules[asset["name"]] = hyperliquid_maintenance(float(asset["maxLeverage"]))
        except httpx.HTTPError as e:
            logger.warning("Hyperliquid meta failed (%s); using default maintenance", type(e).__name__)
        return MarginAccount(Venue.HYPERLIQUID, wallet, positions, schedules)

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """Get current mark price for a symbol."""
        try:
            response = await self.client.post(
                HYPERLIQUID_API_URL,
                json={"type": "allMids"}
            )
            response.raise_for_status()
            mids = response.json()

            clean_symbol = symbol.replace("-PERP", "")

            if clean_symbol in mids:
                return float(mids[clean_symbol])

            return None

        except Exception as e:
            logger.error(f"Error getting mark price for {symbol}: {e}")
            return None

    async def check_key(self) -> KeyCheck:
        """A wallet address grants no permissions; just confirm it is well formed and queryable."""
        if not self.wallet_address.startswith("0x") or len(self.wallet_address) != 42:
            return KeyCheck(False, "Invalid wallet address format")
        try:
            response = await self.client.post(
                HYPERLIQUID_API_URL,
                json={"type": "clearinghouseState", "user": self.wallet_address},
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            return KeyCheck(False, f"Could not query Hyperliquid ({type(e).__name__}). Please try again.")
        return KeyCheck(True, "Wallet address validated (public read-only data)", ["read_positions"], True)

    async def get_account_summary(self) -> dict:
        """Get account summary including margin and balance info."""
        try:
            response = await self.client.post(
                HYPERLIQUID_API_URL,
                json={
                    "type": "clearinghouseState",
                    "user": self.wallet_address
                }
            )
            response.raise_for_status()
            data = response.json()

            margin_summary = data.get("marginSummary", {})

            return {
                "account_value": float(margin_summary.get("accountValue", 0)),
                "total_margin_used": float(margin_summary.get("totalMarginUsed", 0)),
                "total_position_value": float(margin_summary.get("totalNtlPos", 0)),
                "withdrawable": float(data.get("withdrawable", 0)),
            }
        except Exception as e:
            logger.error(f"Error getting account summary: {e}")
            return {}

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()
