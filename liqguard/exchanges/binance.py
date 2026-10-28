"""Binance USDⓈ-M Futures adapter.

Mainnet adapters are strictly read-only: any non-GET request raises
`ReadOnlyViolation` before it leaves the process. Testnet adapters may add
isolated margin, which is what tier-1 protective actions execute.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

from liqguard.models import (
    CROSS,
    ISOLATED,
    MAINNET,
    TESTNET,
    MaintenanceBracket,
    MarginAccount,
    Position,
    Side,
    Venue,
)

from .base import ExchangeAdapter, KeyCheck, ReadOnlyViolation
from .permissions import evaluate_binance_restrictions

logger = logging.getLogger(__name__)

FUTURES_URLS = {
    MAINNET: "https://fapi.binance.com",
    TESTNET: "https://testnet.binancefuture.com",
}
SPOT_API_URL = "https://api.binance.com"
INVALID_KEY_CODES = {-2014, -2015, -1022}


class BinanceAdapter(ExchangeAdapter):
    venue = "binance"

    def __init__(self, api_key: str, api_secret: str, network: str = MAINNET):
        if network not in FUTURES_URLS:
            raise ValueError(f"Unknown network {network!r}")
        self.network = network
        self._api_secret = api_secret
        self._base_url = FUTURES_URLS[network]
        self._client = httpx.AsyncClient(timeout=30.0, headers={"X-MBX-APIKEY": api_key})

    def __repr__(self) -> str:
        return f"BinanceAdapter(network={self.network!r})"

    def _sign(self, params: dict) -> dict:
        params = {**params, "timestamp": int(time.time() * 1000)}
        query = urlencode(params)
        params["signature"] = hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return params

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        signed: bool = False,
        base_url: Optional[str] = None,
    ):
        if method != "GET" and self.network != TESTNET:
            raise ReadOnlyViolation(f"{method} {endpoint} blocked: mainnet adapters are read-only")
        params = self._sign(params or {}) if signed else (params or {})
        url = f"{base_url or self._base_url}{endpoint}"
        response = await self._client.request(method, url, params=params)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _error_code(error: httpx.HTTPStatusError) -> Optional[int]:
        try:
            return int(error.response.json().get("code"))
        except Exception:
            return None

    async def check_key(self) -> KeyCheck:
        if self.network == TESTNET:
            return await self._check_testnet_key()
        try:
            restrictions = await self._request(
                "GET", "/sapi/v1/account/apiRestrictions", signed=True, base_url=SPOT_API_URL
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403) or self._error_code(e) in INVALID_KEY_CODES:
                return KeyCheck(False, "Invalid API key or secret.")
            logger.warning("Binance apiRestrictions returned HTTP %s", e.response.status_code)
            return KeyCheck(False, "Could not verify the key's permissions with Binance. Please try again.")
        except httpx.HTTPError as e:
            logger.warning("Binance apiRestrictions request failed: %s", type(e).__name__)
            return KeyCheck(False, "Could not reach Binance to verify the key. Please try again.")
        return evaluate_binance_restrictions(restrictions, MAINNET)

    async def _check_testnet_key(self) -> KeyCheck:
        try:
            account = await self._request("GET", "/fapi/v2/account", signed=True)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403) or self._error_code(e) in INVALID_KEY_CODES:
                return KeyCheck(False, "Invalid testnet API key or secret.")
            return KeyCheck(False, f"Binance testnet returned HTTP {e.response.status_code}.")
        except httpx.HTTPError as e:
            logger.warning("Binance testnet request failed: %s", type(e).__name__)
            return KeyCheck(False, "Could not reach Binance testnet. Please try again.")
        if account.get("canWithdraw"):
            return KeyCheck(False, "Rejected: testnet key reports withdraw permission.", ["WITHDRAW"], True)
        perms = ["READ", "TESTNET_TRADE"] if account.get("canTrade") else ["READ"]
        return KeyCheck(True, "Testnet key accepted (trading allowed for tier-1 protective actions).", perms, True)

    async def get_positions(self) -> list[Position]:
        try:
            rows = await self._request("GET", "/fapi/v2/positionRisk", signed=True)
        except httpx.HTTPStatusError as e:
            logger.error("Binance positionRisk failed: HTTP %s", e.response.status_code)
            raise

        positions = []
        for row in rows:
            amount = float(row.get("positionAmt", 0))
            if amount == 0:
                continue
            mark = float(row.get("markPrice", 0))
            leverage = float(row.get("leverage", 1)) or 1
            margin = float(row.get("isolatedMargin", 0)) or float(row.get("isolatedWallet", 0))
            if margin == 0:
                margin = abs(amount * mark) / leverage
            isolated = row.get("marginType") == "isolated"
            margin_ratio = None
            if isolated and margin > 0:
                margin_ratio = float(row.get("maintMargin", 0)) / margin * 100
            positions.append(Position(
                venue=Venue.BINANCE,
                symbol=row.get("symbol", ""),
                side=Side.LONG if amount > 0 else Side.SHORT,
                size=abs(amount),
                entry_price=float(row.get("entryPrice", 0)),
                mark_price=mark,
                liquidation_price=float(row.get("liquidationPrice", 0)),
                margin=margin,
                leverage=leverage,
                unrealized_pnl=float(row.get("unRealizedProfit", 0)),
                margin_ratio=margin_ratio,
                network=self.network,
                margin_mode=ISOLATED if isolated else CROSS,
                isolated_wallet=float(row.get("isolatedWallet", 0)) if isolated else None,
            ))
        return positions

    async def _cross_wallet_balance(self) -> float:
        account = await self._request("GET", "/fapi/v2/account", signed=True)
        for asset in account.get("assets", []):
            if asset.get("asset") == "USDT":
                return float(asset.get("crossWalletBalance", 0))
        return float(account.get("totalCrossWalletBalance", 0))

    async def _brackets(self, symbols: set[str]) -> dict[str, tuple[MaintenanceBracket, ...]]:
        rows = await self._request("GET", "/fapi/v1/leverageBracket", signed=True)
        if isinstance(rows, dict):
            rows = [rows]
        out = {}
        for row in rows:
            if row.get("symbol") not in symbols:
                continue
            tiers = sorted(row.get("brackets", []), key=lambda b: float(b["notionalFloor"]))
            out[row["symbol"]] = tuple(
                MaintenanceBracket(float(b["notionalFloor"]), float(b["notionalCap"]), float(b["maintMarginRatio"]), float(b.get("cum", 0)))
                for b in tiers
            )
        return out

    async def get_account(self) -> MarginAccount:
        """Positions, USDT cross wallet balance and this account's maintenance tiers (all read-only GETs)."""
        positions = await self.get_positions()
        wallet = await self._cross_wallet_balance()
        schedules = {}
        if positions:
            try:
                schedules = await self._brackets({p.symbol for p in positions})
            except httpx.HTTPError as e:
                logger.warning("Binance leverageBracket failed (%s); using fallback tiers", type(e).__name__)
        return MarginAccount(Venue.BINANCE, wallet, positions, schedules, network=self.network)

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        try:
            data = await self._request("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol})
            return float(data.get("markPrice", 0)) or None
        except httpx.HTTPError as e:
            logger.warning("Binance mark price for %s failed: %s", symbol, type(e).__name__)
            return None

    async def add_isolated_margin(self, symbol: str, amount_usd: float, position_side: str = "BOTH") -> tuple[bool, str]:
        """Add isolated margin to a position. Testnet only (mainnet raises ReadOnlyViolation)."""
        params = {"symbol": symbol, "amount": f"{amount_usd:.2f}", "type": 1, "positionSide": position_side}
        try:
            result = await self._request("POST", "/fapi/v1/positionMargin", params=params, signed=True)
        except httpx.HTTPStatusError as e:
            code = self._error_code(e)
            return False, f"Binance testnet rejected the request (HTTP {e.response.status_code}, code {code})"
        except httpx.HTTPError as e:
            return False, f"Binance testnet unreachable ({type(e).__name__})"
        if int(result.get("code", 200)) == 200:
            return True, f"Added ${amount_usd:,.2f} isolated margin to {symbol} on Binance testnet"
        return False, str(result.get("msg", "unknown error"))

    async def close(self) -> None:
        await self._client.aclose()
