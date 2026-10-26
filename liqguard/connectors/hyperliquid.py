"""Hyperliquid connector using public API (no key required)."""

import logging
from typing import Optional

import httpx

from liqguard.models import Position, Side, Venue

from .base import BaseConnector

logger = logging.getLogger(__name__)

HYPERLIQUID_API_URL = "https://api.hyperliquid.xyz/info"


class HyperliquidConnector(BaseConnector):
    """
    Connector for Hyperliquid DEX.

    Uses public API - only requires wallet address, no private key needed.
    Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
    """

    def __init__(self, wallet_address: str):
        """
        Initialize Hyperliquid connector.

        Args:
            wallet_address: User's wallet address (0x...)
        """
        self.wallet_address = wallet_address.lower()
        self.client = httpx.AsyncClient(timeout=30.0)
        self._meta_cache: Optional[dict] = None

    @property
    def venue_name(self) -> str:
        return "hyperliquid"

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

    async def get_positions(self) -> list[Position]:
        """Fetch all open positions for the wallet address."""
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

            positions = []
            asset_positions = data.get("assetPositions", [])

            meta = await self._get_meta()
            {asset["name"]: asset for asset in meta.get("universe", [])}

            for pos_data in asset_positions:
                pos = pos_data.get("position", {})

                if float(pos.get("szi", 0)) == 0:
                    continue

                coin = pos.get("coin", "")
                size = float(pos.get("szi", 0))
                entry_price = float(pos.get("entryPx", 0))

                mark_price = await self.get_mark_price(coin)
                if mark_price is None:
                    logger.warning(f"Could not get mark price for {coin}")
                    continue

                liquidation_price = float(pos.get("liquidationPx", 0)) if pos.get("liquidationPx") else 0
                margin = float(pos.get("marginUsed", 0))
                leverage = float(pos.get("leverage", {}).get("value", 1)) if isinstance(pos.get("leverage"), dict) else float(pos.get("leverage", 1))
                unrealized_pnl = float(pos.get("unrealizedPnl", 0))

                margin_ratio = None
                if margin > 0:
                    margin_ratio = (abs(unrealized_pnl) / margin) * 100 if unrealized_pnl < 0 else 0

                position = Position(
                    venue=Venue.HYPERLIQUID,
                    symbol=f"{coin}-PERP",
                    side=Side.LONG if size > 0 else Side.SHORT,
                    size=abs(size),
                    entry_price=entry_price,
                    mark_price=mark_price,
                    liquidation_price=liquidation_price,
                    margin=margin,
                    leverage=leverage,
                    unrealized_pnl=unrealized_pnl,
                    margin_ratio=margin_ratio,
                )
                positions.append(position)

            return positions

        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching Hyperliquid positions: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching Hyperliquid positions: {e}")
            raise

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

    async def validate_credentials(self) -> tuple[bool, str, list[str]]:
        """
        Validate the wallet address.

        For Hyperliquid, we just check if the address format is valid
        and if we can fetch data for it.
        """
        if not self.wallet_address.startswith("0x") or len(self.wallet_address) != 42:
            return False, "Invalid wallet address format", []

        try:
            response = await self.client.post(
                HYPERLIQUID_API_URL,
                json={
                    "type": "clearinghouseState",
                    "user": self.wallet_address
                }
            )
            response.raise_for_status()
            return True, "Wallet address validated successfully", ["read_positions"]
        except Exception as e:
            return False, f"Failed to validate wallet: {str(e)}", []

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
