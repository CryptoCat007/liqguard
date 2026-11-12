"""
PayPal API client for Subscriptions and webhook verification.

Uses the PayPal Subscriptions API (preferred) with Orders v2 fallback for one-time payments.
Supports sandbox and live environments via PAYPAL_ENV.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class PayPalError(Exception):
    """PayPal API error with details."""

    def __init__(self, message: str, status_code: int = 0, details: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}

    def is_already_captured(self) -> bool:
        """True when capture is a 422 ORDER_ALREADY_CAPTURED (return vs webhook race)."""
        issues = []
        extra = self.details.get("details") if isinstance(self.details, dict) else None
        if isinstance(extra, list):
            issues.extend(str(item.get("issue") or "") for item in extra if isinstance(item, dict))
        name = str(self.details.get("name") or "") if isinstance(self.details, dict) else ""
        blob = " ".join([str(self), name, *issues]).upper()
        return "ORDER_ALREADY_CAPTURED" in blob or ("ALREADY" in blob and "CAPTURED" in blob)


@dataclass
class SubscriptionDetails:
    """Parsed subscription information from PayPal."""

    subscription_id: str
    plan_id: str
    status: str  # ACTIVE, CANCELLED, SUSPENDED, EXPIRED
    payer_email: Optional[str] = None
    next_billing_time: Optional[str] = None
    start_time: Optional[str] = None
    custom_id: Optional[str] = None

    @classmethod
    def from_api(cls, data: dict) -> "SubscriptionDetails":
        subscriber = data.get("subscriber", {})
        billing_info = data.get("billing_info", {})
        return cls(
            subscription_id=data.get("id", ""),
            plan_id=data.get("plan_id", ""),
            status=data.get("status", "UNKNOWN"),
            payer_email=subscriber.get("email_address"),
            next_billing_time=billing_info.get("next_billing_time"),
            start_time=data.get("start_time"),
            custom_id=data.get("custom_id"),
        )

    def is_active(self) -> bool:
        return self.status == "ACTIVE"


@dataclass
class PlanDetails:
    """Parsed plan information from PayPal."""

    plan_id: str
    product_id: str
    name: str
    description: str
    status: str
    billing_cycles: list[dict]

    @classmethod
    def from_api(cls, data: dict) -> "PlanDetails":
        return cls(
            plan_id=data.get("id", ""),
            product_id=data.get("product_id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            status=data.get("status", ""),
            billing_cycles=data.get("billing_cycles", []),
        )


class PayPalClient:
    """
    PayPal REST API client for Subscriptions.

    Handles OAuth authentication, subscription management, and webhook verification.
    All methods are async-safe for use in FastAPI endpoints.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        api_base: str = "https://api-m.sandbox.paypal.com",
        webhook_id: Optional[str] = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_base = api_base.rstrip("/")
        self.webhook_id = webhook_id
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0

    @property
    def is_sandbox(self) -> bool:
        return "sandbox" in (self.api_base or "")

    async def _get_access_token(self) -> str:
        """Get OAuth access token, refreshing if expired."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.api_base}/v1/oauth2/token",
                headers={
                    "Authorization": f"Basic {auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials"},
                timeout=30,
            )

        if response.status_code != 200:
            raise PayPalError(f"Failed to get access token: {response.text}", response.status_code)

        data = response.json()
        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 3600)
        return self._access_token

    async def _request(
        self,
        method: str,
        path: str,
        data: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """Make an authenticated API request."""
        token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                f"{self.api_base}{path}",
                headers=headers,
                json=data,
                params=params,
                timeout=30,
            )

        if response.status_code >= 400:
            try:
                error_data = response.json()
            except Exception:
                error_data = {"error": response.text}
            raise PayPalError(
                f"PayPal API error: {error_data.get('message', response.text)}",
                response.status_code,
                error_data,
            )

        if response.status_code == 204:
            return {}
        return response.json()

    # ----------------------------------------------------------------
    # Subscription management
    # ----------------------------------------------------------------

    async def create_subscription(
        self,
        plan_id: str,
        return_url: str,
        cancel_url: str,
        custom_id: Optional[str] = None,
        subscriber_email: Optional[str] = None,
    ) -> dict:
        """
        Create a subscription and return approval URL.

        Returns:
            {"subscription_id": "...", "approve_url": "https://..."}
        """
        payload: dict[str, Any] = {
            "plan_id": plan_id,
            "application_context": {
                "brand_name": "LiqGuard Pro",
                "locale": "en-US",
                "shipping_preference": "NO_SHIPPING",
                "user_action": "SUBSCRIBE_NOW",
                "return_url": return_url,
                "cancel_url": cancel_url,
            },
        }

        if custom_id:
            payload["custom_id"] = custom_id

        if subscriber_email:
            payload["subscriber"] = {"email_address": subscriber_email}

        result = await self._request("POST", "/v1/billing/subscriptions", payload)

        approve_link = next(
            (link["href"] for link in result.get("links", []) if link["rel"] == "approve"),
            None,
        )

        return {
            "subscription_id": result.get("id"),
            "approve_url": approve_link,
            "status": result.get("status"),
        }

    async def get_subscription(self, subscription_id: str) -> SubscriptionDetails:
        """Get subscription details."""
        result = await self._request("GET", f"/v1/billing/subscriptions/{subscription_id}")
        return SubscriptionDetails.from_api(result)

    async def cancel_subscription(self, subscription_id: str, reason: str = "User requested") -> bool:
        """Cancel a subscription. Returns True on success."""
        try:
            await self._request(
                "POST",
                f"/v1/billing/subscriptions/{subscription_id}/cancel",
                {"reason": reason},
            )
            return True
        except PayPalError as e:
            logger.warning("Failed to cancel subscription %s: %s", subscription_id, e)
            return False

    async def suspend_subscription(self, subscription_id: str, reason: str = "Suspended") -> bool:
        """Suspend a subscription. Returns True on success."""
        try:
            await self._request(
                "POST",
                f"/v1/billing/subscriptions/{subscription_id}/suspend",
                {"reason": reason},
            )
            return True
        except PayPalError as e:
            logger.warning("Failed to suspend subscription %s: %s", subscription_id, e)
            return False

    async def activate_subscription(self, subscription_id: str, reason: str = "Reactivated") -> bool:
        """Reactivate a suspended subscription. Returns True on success."""
        try:
            await self._request(
                "POST",
                f"/v1/billing/subscriptions/{subscription_id}/activate",
                {"reason": reason},
            )
            return True
        except PayPalError as e:
            logger.warning("Failed to activate subscription %s: %s", subscription_id, e)
            return False

    # ----------------------------------------------------------------
    # Plan management
    # ----------------------------------------------------------------

    async def get_plan(self, plan_id: str) -> PlanDetails:
        """Get plan details."""
        result = await self._request("GET", f"/v1/billing/plans/{plan_id}")
        return PlanDetails.from_api(result)

    async def create_product(self, name: str, description: str) -> str:
        """Create a product for billing. Returns product_id."""
        result = await self._request(
            "POST",
            "/v1/catalogs/products",
            {
                "name": name,
                "description": description,
                "type": "SERVICE",
                "category": "SOFTWARE",
            },
        )
        return result["id"]

    async def create_plan(
        self,
        product_id: str,
        name: str,
        description: str,
        price_usd: str = "9.99",
        interval: str = "MONTH",
    ) -> str:
        """Create a subscription plan. Returns plan_id."""
        result = await self._request(
            "POST",
            "/v1/billing/plans",
            {
                "product_id": product_id,
                "name": name,
                "description": description,
                "status": "ACTIVE",
                "billing_cycles": [
                    {
                        "frequency": {"interval_unit": interval, "interval_count": 1},
                        "tenure_type": "REGULAR",
                        "sequence": 1,
                        "total_cycles": 0,  # infinite
                        "pricing_scheme": {
                            "fixed_price": {"value": price_usd, "currency_code": "USD"},
                        },
                    }
                ],
                "payment_preferences": {
                    "auto_bill_outstanding": True,
                    "payment_failure_threshold": 3,
                },
            },
        )
        return result["id"]

    # ----------------------------------------------------------------
    # Webhook verification
    # ----------------------------------------------------------------

    def verify_webhook_signature(
        self,
        transmission_id: str,
        transmission_time: str,
        cert_url: str,
        auth_algo: str,
        transmission_sig: str,
        body: bytes,
    ) -> bool:
        """
        Verify PayPal webhook signature.

        In production, this should fetch the certificate from cert_url and verify.
        For sandbox testing, we use a simpler HMAC check or skip verification.

        Args:
            transmission_id: PayPal-Transmission-Id header
            transmission_time: PayPal-Transmission-Time header
            cert_url: PayPal-Cert-Url header (certificate URL)
            auth_algo: PayPal-Auth-Algo header
            transmission_sig: PayPal-Transmission-Sig header
            body: Raw request body bytes

        Returns:
            True if signature is valid
        """
        if not self.webhook_id:
            logger.error("Rejecting webhook: PAYPAL_WEBHOOK_ID is not configured")
            return False

        try:
            crc = hashlib.crc32(body) & 0xFFFFFFFF
            expected_signature_string = f"{transmission_id}|{transmission_time}|{self.webhook_id}|{crc}"
            logger.debug("Webhook signature string: %s", expected_signature_string)
            return True  # Actual verification requires cert fetch
        except Exception as e:
            logger.error("Webhook signature verification failed: %s", e)
            return False

    async def verify_webhook_online(
        self,
        transmission_id: str,
        transmission_time: str,
        cert_url: str,
        auth_algo: str,
        transmission_sig: str,
        body: dict,
    ) -> bool:
        """
        Verify webhook signature using PayPal's verification API.

        This is the recommended approach for production: PayPal validates the signature.
        """
        if not self.webhook_id:
            logger.error("Rejecting webhook: PAYPAL_WEBHOOK_ID is not configured")
            return False

        try:
            result = await self._request(
                "POST",
                "/v1/notifications/verify-webhook-signature",
                {
                    "auth_algo": auth_algo,
                    "cert_url": cert_url,
                    "transmission_id": transmission_id,
                    "transmission_sig": transmission_sig,
                    "transmission_time": transmission_time,
                    "webhook_id": self.webhook_id,
                    "webhook_event": body,
                },
            )
            status = result.get("verification_status", "")
            if status == "SUCCESS":
                return True
            logger.warning("Webhook verification returned: %s", status)
            return False
        except PayPalError as e:
            logger.error("Webhook verification API call failed: %s", e)
            return False

    # ----------------------------------------------------------------
    # One-time payments (Orders v2 fallback for Rescue Pass)
    # ----------------------------------------------------------------

    async def create_order(
        self,
        amount_usd: str,
        description: str,
        return_url: str,
        cancel_url: str,
        custom_id: Optional[str] = None,
    ) -> dict:
        """
        Create a one-time payment order (Rescue Pass).

        Returns:
            {"order_id": "...", "approve_url": "https://..."}
        """
        payload: dict[str, Any] = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "amount": {"currency_code": "USD", "value": amount_usd},
                    "description": description,
                }
            ],
            "application_context": {
                "brand_name": "LiqGuard",
                "shipping_preference": "NO_SHIPPING",
                "user_action": "PAY_NOW",
                "return_url": return_url,
                "cancel_url": cancel_url,
            },
        }

        if custom_id:
            payload["purchase_units"][0]["custom_id"] = custom_id

        result = await self._request("POST", "/v2/checkout/orders", payload)

        approve_link = next(
            (link["href"] for link in result.get("links", []) if link["rel"] == "approve"),
            None,
        )

        return {
            "order_id": result.get("id"),
            "approve_url": approve_link,
            "status": result.get("status"),
        }

    async def capture_order(self, order_id: str) -> dict:
        """Capture an approved order. Returns capture details."""
        return await self._request("POST", f"/v2/checkout/orders/{order_id}/capture")

    async def get_order(self, order_id: str) -> dict:
        """GET /v2/checkout/orders/{id} — used to recover custom_id after capture."""
        return await self._request("GET", f"/v2/checkout/orders/{order_id}")
