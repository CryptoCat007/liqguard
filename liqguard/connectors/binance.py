"""Binance USDⓈ-M Futures connector with read-only key validation."""

import hashlib
import hmac
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

from liqguard.models import Position, Side, Venue

from .base import BaseConnector

logger = logging.getLogger(__name__)

BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"


class BinanceConnector(BaseConnector):
    """
    Connector for Binance USDⓈ-M Futures.

    Requires API key with read-only permissions. Will warn/reject keys
    with trade or withdraw permissions.
    """

    DANGEROUS_PERMISSIONS = ["SPOT", "MARGIN", "FUTURES", "WITHDRAWALS", "INTERNAL_TRANSFER"]
    SAFE_PERMISSIONS = ["READ_INFO", "ENABLE_READING"]

    def __init__(self, api_key: str, api_secret: str):
        """
        Initialize Binance connector.

        Args:
            api_key: Binance API key
            api_secret: Binance API secret
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={"X-MBX-APIKEY": api_key}
        )
        self._exchange_info_cache: Optional[dict] = None

    @property
    def venue_name(self) -> str:
        return "binance"

    def _sign_request(self, params: dict) -> dict:
        """Sign request parameters with HMAC SHA256."""
        params["timestamp"] = int(time.time() * 1000)
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        signed: bool = False
    ) -> dict:
        """Make a request to Binance API."""
        url = f"{BINANCE_FUTURES_BASE_URL}{endpoint}"
        params = params or {}

        if signed:
            params = self._sign_request(params)

        if method == "GET":
            response = await self.client.get(url, params=params)
        else:
            response = await self.client.post(url, params=params)

        response.raise_for_status()
        return response.json()

    async def validate_credentials(self) -> tuple[bool, str, list[str]]:
        """
        Validate API credentials and check permissions.

        Returns:
            Tuple of (is_valid, message, detected_permissions)

        Will warn if dangerous permissions are detected.
        """
        try:
            account_info = await self._request(
                "GET", "/fapi/v2/account", signed=True
            )

            detected_permissions = []

            can_trade = account_info.get("canTrade", False)
            can_deposit = account_info.get("canDeposit", False)
            can_withdraw = account_info.get("canWithdraw", False)

            warnings = []

            if can_trade:
                warnings.append("TRADE permission detected")
                detected_permissions.append("FUTURES_TRADE")
            if can_deposit:
                warnings.append("DEPOSIT permission detected")
                detected_permissions.append("DEPOSIT")
            if can_withdraw:
                warnings.append("WITHDRAW permission detected - DANGEROUS!")
                detected_permissions.append("WITHDRAW")

            detected_permissions.append("READ_INFO")

            if can_withdraw:
                return (
                    False,
                    "API key has WITHDRAW permission. This is dangerous! Please create a new key with READ-ONLY permissions.",
                    detected_permissions
                )

            if can_trade:
                return (
                    True,
                    f"WARNING: API key has TRADE permission. Recommend using read-only key. Detected: {', '.join(warnings)}",
                    detected_permissions
                )

            return (
                True,
                "API key validated successfully with read-only permissions",
                detected_permissions
            )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                return False, "Invalid API key", []
            elif e.response.status_code == 403:
                return False, "API key does not have futures permissions", []
            else:
                return False, f"API error: {e.response.status_code}", []
        except Exception as e:
            return False, f"Validation failed: {str(e)}", []

    async def get_positions(self) -> list[Position]:
        """Fetch all open positions."""
        try:
            positions_data = await self._request(
                "GET", "/fapi/v2/positionRisk", signed=True
            )

            mark_prices = await self._get_all_mark_prices()

            positions = []

            for pos in positions_data:
                position_amt = float(pos.get("positionAmt", 0))

                if position_amt == 0:
                    continue

                symbol = pos.get("symbol", "")
                entry_price = float(pos.get("entryPrice", 0))
                mark_price = float(pos.get("markPrice", 0)) or mark_prices.get(symbol, 0)
                liquidation_price = float(pos.get("liquidationPrice", 0))
                leverage = float(pos.get("leverage", 1))
                unrealized_pnl = float(pos.get("unRealizedProfit", 0))

                isolated_margin = float(pos.get("isolatedMargin", 0))
                isolated_wallet = float(pos.get("isolatedWallet", 0))
                margin = isolated_margin if isolated_margin > 0 else isolated_wallet

                if margin == 0:
                    notional = abs(position_amt * mark_price)
                    margin = notional / leverage

                margin_ratio = None
                if pos.get("marginType") == "isolated" and margin > 0:
                    maintenance_margin = float(pos.get("maintMargin", 0))
                    margin_ratio = (maintenance_margin / margin) * 100 if margin > 0 else None

                position = Position(
                    venue=Venue.BINANCE,
                    symbol=symbol,
                    side=Side.LONG if position_amt > 0 else Side.SHORT,
                    size=abs(position_amt),
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
            logger.error(f"HTTP error fetching Binance positions: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching Binance positions: {e}")
            raise

    async def _get_all_mark_prices(self) -> dict[str, float]:
        """Get mark prices for all symbols."""
        try:
            data = await self._request("GET", "/fapi/v1/premiumIndex")
            return {item["symbol"]: float(item["markPrice"]) for item in data}
        except Exception as e:
            logger.error(f"Error fetching mark prices: {e}")
            return {}

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """Get current mark price for a symbol."""
        try:
            data = await self._request(
                "GET", "/fapi/v1/premiumIndex",
                params={"symbol": symbol}
            )
            return float(data.get("markPrice", 0))
        except Exception as e:
            logger.error(f"Error getting mark price for {symbol}: {e}")
            return None

    async def get_account_balance(self) -> dict:
        """Get account balance summary."""
        try:
            account = await self._request("GET", "/fapi/v2/account", signed=True)

            return {
                "total_wallet_balance": float(account.get("totalWalletBalance", 0)),
                "total_unrealized_profit": float(account.get("totalUnrealizedProfit", 0)),
                "total_margin_balance": float(account.get("totalMarginBalance", 0)),
                "available_balance": float(account.get("availableBalance", 0)),
                "max_withdraw_amount": float(account.get("maxWithdrawAmount", 0)),
            }
        except Exception as e:
            logger.error(f"Error getting account balance: {e}")
            return {}

    async def get_exchange_info(self) -> dict:
        """Get exchange info (symbol specs)."""
        if self._exchange_info_cache is not None:
            return self._exchange_info_cache

        try:
            self._exchange_info_cache = await self._request(
                "GET", "/fapi/v1/exchangeInfo"
            )
            return self._exchange_info_cache
        except Exception as e:
            logger.error(f"Error getting exchange info: {e}")
            return {}

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()
