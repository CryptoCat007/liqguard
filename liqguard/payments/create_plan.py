"""
Create a PayPal sandbox product + monthly Pro plan.

Reads PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET / PAYPAL_ENV from the environment.
Prints the plan id to set as PAYPAL_PLAN_ID. Never logs secrets.

    python3 -m liqguard.payments.create_plan
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

from liqguard.payments import PayPalClient, PayPalError


async def main() -> int:
    load_dotenv()
    client_id = os.environ.get("PAYPAL_CLIENT_ID") or ""
    secret = os.environ.get("PAYPAL_CLIENT_SECRET") or ""
    env = (os.environ.get("PAYPAL_ENV") or "sandbox").lower()
    if not client_id or not secret:
        print("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in the environment (see .env.example).", file=sys.stderr)
        return 2
    if env == "live":
        print("Refusing to create a plan against live PayPal. Use PAYPAL_ENV=sandbox.", file=sys.stderr)
        return 2
    client = PayPalClient(client_id, secret, "https://api-m.sandbox.paypal.com")
    try:
        product_id = await client.create_product("LiqGuard Pro", "Faster alerts, multi-account, unlimited Muse rescue advice")
        plan_id = await client.create_plan(product_id, "LiqGuard Pro monthly", "LiqGuard Pro subscription", "9.99")
    except PayPalError as e:
        print(f"PayPal error: {e}", file=sys.stderr)
        return 1
    print(f"PAYPAL_PLAN_ID={plan_id}")
    print("Create a webhook at https://developer.paypal.com/dashboard/webhooks pointing to")
    print("  https://<your-domain>/webhooks/paypal")
    print("Subscribe to BILLING.SUBSCRIPTION.ACTIVATED / CANCELLED / SUSPENDED / PAYMENT.FAILED")
    print("and PAYMENT.CAPTURE.COMPLETED, then set PAYPAL_WEBHOOK_ID.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
