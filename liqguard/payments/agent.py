"""
Muse as a PayPal payment agent.

Tool names and shapes follow PayPal's Agent Toolkit
(https://developer.paypal.com/ai-tools/toolkit): `create_order` and
`create_subscription`. Execution always goes through our sandbox REST client
with **server-side fixed prices** — the model cannot choose an amount, fund a
trading account, or buy crypto. The user must approve the PayPal link; nothing
is captured here.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .paypal_client import PayPalClient, PayPalError

logger = logging.getLogger("liqguard.payments.agent")

RESCUE_PASS_USD = "2.99"
RESCUE_PASS_DESCRIPTION = "LiqGuard Rescue Pass"
ALLOWED_TOOLS = frozenset({"create_order", "create_subscription"})
MUSE_CHECKOUT_TIMEOUT = 8.0

# PayPal Agent Toolkit–compatible OpenAI function tools. Amounts are omitted on
# purpose: the host fills them in from RESCUE_PASS_USD / PAYPAL_PLAN_ID.
OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_order",
            "description": (
                "Create a PayPal sandbox order for LiqGuard's one-time Rescue Pass. "
                "Use when the user wants a Rescue Pass or unlimited Muse advice today. "
                "Do not set an amount; the server uses a fixed price. "
                "Never use this to fund a trading account or buy crypto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "Must be rescue_pass",
                        "enum": ["rescue_pass"],
                    }
                },
                "required": ["intent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_subscription",
            "description": (
                "Create a PayPal sandbox subscription for LiqGuard Pro. "
                "Use when the user wants to upgrade to Pro. "
                "The server uses the configured PAYPAL_PLAN_ID. "
                "Never use this to fund a trading account or buy crypto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "Must be liqguard_pro",
                        "enum": ["liqguard_pro"],
                    }
                },
                "required": ["intent"],
            },
        },
    },
]

_RESCUE = re.compile(
    r"rescue\s*pass|buy (a |the )?pass|get me a rescue|救援通行證|救援通|買.*通行證",
    re.I,
)
_PRO = re.compile(
    r"upgrade(\s+to)?\s+pro|subscribe(\s+to)?\s+pro|liqguard pro|升級.*pro|訂閱.*pro|开通.*pro|開通.*pro",
    re.I,
)


@dataclass
class MusePick:
    """Muse (or rules) choice for which PayPal tool to run."""

    product: Optional[str]
    source: str  # muse | rules | timeout
    note: str = ""


@dataclass
class AgentCheckout:
    """Result of one agent tool call (or a deterministic fallback)."""

    tool: Optional[str]
    product: Optional[str]  # rescue | pro
    source: str  # muse | rules | timeout
    approve_url: Optional[str] = None
    resource_id: Optional[str] = None
    status: str = "created"  # created | error | none | rejected
    message: str = ""
    note: str = ""
    model_args: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "product": self.product,
            "source": self.source,
            "approve_url": self.approve_url,
            "resource_id": self.resource_id,
            "status": self.status,
            "message": self.message,
            "note": self.note,
        }


def intent_from_text(text: str) -> Optional[str]:
    """Deterministic checkout intent: 'rescue', 'pro', or None. Never reads amounts."""
    blob = (text or "").strip()
    if not blob:
        return None
    rescue, pro = bool(_RESCUE.search(blob)), bool(_PRO.search(blob))
    if rescue and not pro:
        return "rescue"
    if pro and not rescue:
        return "pro"
    if rescue and pro:
        return "rescue" if blob.lower().find("rescue") >= 0 or "救援" in blob else "pro"
    return None


def tool_for_product(product: str) -> str:
    return "create_order" if product == "rescue" else "create_subscription"


def _log(user_id: int, checkout: AgentCheckout) -> None:
    logger.info(
        "PayPal agent tool=%s source=%s user=%s product=%s status=%s resource=%s",
        checkout.tool, checkout.source, user_id, checkout.product, checkout.status, checkout.resource_id,
    )


def _muse_note(content: Optional[str]) -> str:
    note = " ".join((content or "").split())
    if len(note) > 240:
        return note[:237] + "..."
    return note


async def muse_pick_tool(llm: Any, text: str, lang: str) -> MusePick:
    """
    Ask Muse which PayPal Agent Toolkit tool to call.

    Returns MusePick(product, source, note) where product is rescue|pro|None
    and source is muse|rules.
    """
    fallback = intent_from_text(text)
    if llm is None or getattr(llm, "is_mock", True):
        return MusePick(fallback, "rules")
    if not hasattr(llm, "_create") and getattr(llm, "client", None) is None:
        return MusePick(fallback, "rules")
    system = (
        "You are LiqGuard's payment agent. You may only call PayPal tools for "
        "LiqGuard's own Rescue Pass (create_order) or Pro subscription "
        "(create_subscription). Never send money to a trading account or buy crypto. "
        "If the user tapped Rescue Pass on a liquidation-risk alert, call create_order "
        "and write one short line on why Rescue Pass fits now. "
        "If they tapped Pro, call create_subscription and write one short line. "
        "If the user is not asking to pay, do not call a tool. "
        f"Reply in {'Traditional Chinese' if str(lang).startswith('zh') else 'English'}."
    )
    try:
        kwargs: dict[str, Any] = {
            "model": getattr(llm, "model", "muse-spark-1.3"),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "tools": OPENAI_TOOLS,
            "tool_choice": "auto",
            "temperature": 0,
            "max_tokens": 400,
        }
        if hasattr(llm, "_create"):
            response = await llm._create(kwargs)
        else:
            response = await llm.client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        note = _muse_note(getattr(msg, "content", None))
        calls = getattr(msg, "tool_calls", None) or []
        if calls:
            name = calls[0].function.name
            if name not in ALLOWED_TOOLS:
                logger.warning("PayPal agent refused unknown Muse tool %s", name)
                return MusePick(None, "muse", note)
            if name == "create_order":
                return MusePick("rescue", "muse", note)
            if name == "create_subscription":
                return MusePick("pro", "muse", note)
        content = (msg.content or "").lower()
        if "create_order" in content or "rescue" in content:
            return MusePick("rescue", "muse", note)
        if "create_subscription" in content or "pro" in content:
            return MusePick("pro", "muse", note)
    except Exception as e:
        logger.warning("Muse PayPal tool choice failed (%s); using rules", type(e).__name__)
    return MusePick(fallback, "rules")


async def execute_tool(
    paypal: PayPalClient,
    product: str,
    *,
    user_id: int,
    plan_id: Optional[str],
    return_url: str,
    cancel_url: str,
    model_args: Optional[dict] = None,
) -> AgentCheckout:
    """Run a PayPal Agent Toolkit tool with host-fixed prices. Ignores model amounts."""
    model_args = dict(model_args or {})
    model_args.pop("amount", None)
    model_args.pop("value", None)
    model_args.pop("currency", None)

    if "sandbox" not in (paypal.api_base or ""):
        checkout = AgentCheckout(
            tool=tool_for_product(product) if product in ("rescue", "pro") else None,
            product=product if product in ("rescue", "pro") else None,
            source="rules",
            status="rejected",
            message="Agent checkout is sandbox-only",
            model_args=model_args,
        )
        _log(user_id, checkout)
        return checkout

    if product == "rescue":
        checkout = AgentCheckout(tool="create_order", product="rescue", source="rules", model_args=model_args)
        try:
            result = await paypal.create_order(
                amount_usd=RESCUE_PASS_USD,
                description=RESCUE_PASS_DESCRIPTION,
                return_url=return_url,
                cancel_url=cancel_url,
                custom_id=str(user_id),
            )
            checkout.approve_url = result.get("approve_url")
            checkout.resource_id = result.get("order_id")
            checkout.status = "created" if checkout.approve_url else "error"
            checkout.message = "Rescue Pass order created" if checkout.approve_url else "PayPal did not return an approve URL"
        except PayPalError as e:
            checkout.status = "error"
            checkout.message = str(e)
        _log(user_id, checkout)
        return checkout

    if product == "pro":
        checkout = AgentCheckout(tool="create_subscription", product="pro", source="rules", model_args=model_args)
        if not plan_id:
            checkout.status = "error"
            checkout.message = "PAYPAL_PLAN_ID is not configured"
            _log(user_id, checkout)
            return checkout
        try:
            result = await paypal.create_subscription(
                plan_id=plan_id,
                return_url=return_url,
                cancel_url=cancel_url,
                custom_id=str(user_id),
            )
            checkout.approve_url = result.get("approve_url")
            checkout.resource_id = result.get("subscription_id")
            checkout.status = "created" if checkout.approve_url else "error"
            checkout.message = "Pro subscription created" if checkout.approve_url else "PayPal did not return an approve URL"
        except PayPalError as e:
            checkout.status = "error"
            checkout.message = str(e)
        _log(user_id, checkout)
        return checkout

    checkout = AgentCheckout(tool=None, product=None, source="rules", status="rejected",
                             message="Agent may only create a Rescue Pass order or Pro subscription")
    _log(user_id, checkout)
    return checkout


async def run_checkout(
    paypal: Optional[PayPalClient],
    llm: Any,
    *,
    user_id: int,
    text: str,
    lang: str,
    plan_id: Optional[str],
    dashboard_url: str,
    product: Optional[str] = None,
    store: Any = None,
    default_product: Optional[str] = None,
) -> AgentCheckout:
    """Pick a tool (Muse or rules) and execute it.

    `product` still forces Rescue Pass / Pro (prices stay server-side), but Muse
    is always asked so px/rescue and px/pro record source=muse when it answers.
    """
    pick = MusePick(intent_from_text(text), "rules")
    try:
        pick = await asyncio.wait_for(muse_pick_tool(llm, text, lang), MUSE_CHECKOUT_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Muse PayPal tool choice timed out after %.0fs", MUSE_CHECKOUT_TIMEOUT)
        pick = MusePick(intent_from_text(text), "timeout")
    except Exception as e:
        logger.warning("Muse PayPal tool choice failed (%s); using rules", type(e).__name__)
        pick = MusePick(intent_from_text(text), "rules")

    forced = product in ("rescue", "pro")
    if not forced:
        product = pick.product
        if product is None and default_product in ("rescue", "pro"):
            product = default_product
    source = pick.source if pick.source in ("muse", "timeout") else "rules"
    note = pick.note if source == "muse" else ""
    if product is None:
        checkout = AgentCheckout(tool=None, product=None, source=source, status="none",
                                 message="No LiqGuard PayPal checkout requested", note=note)
        _log(user_id, checkout)
        return checkout
    if paypal is None:
        checkout = AgentCheckout(
            tool=tool_for_product(product), product=product, source=source, status="error",
            message="PayPal is not configured", note=note,
        )
        _log(user_id, checkout)
        if store is not None:
            await store.save_agent_action(user_id, checkout)
        return checkout
    base = dashboard_url.rstrip("/")
    return_url = f"{base}/api/me/billing/rescue-return" if product == "rescue" else f"{base}/api/me/billing/return"
    checkout = await execute_tool(
        paypal, product, user_id=user_id, plan_id=plan_id,
        return_url=return_url, cancel_url=f"{base}/me",
    )
    checkout.source = source
    checkout.note = note
    if store is not None:
        await store.save_agent_action(user_id, checkout)
    return checkout


def parse_grid_filter(query: str) -> dict:
    """Deterministic NL filter for the positions AG Grid. Muse can refine this later."""
    q = (query or "").strip()
    if not q:
        return {}
    out: dict[str, Any] = {"query": q}
    m = re.search(r"(?:within|under|below|距離|以內|內)\s*(\d+(?:\.\d+)?)\s*%", q, re.I)
    if not m:
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", q)
    if m:
        out["max_distance_pct"] = float(m.group(1))
    if re.search(r"\blong\b|多單|做多", q, re.I):
        out["side"] = "long"
    if re.search(r"\bshort\b|空單|做空", q, re.I):
        out["side"] = "short"
    for level in ("emergency", "critical", "warning", "safe"):
        if re.search(rf"\b{level}\b", q, re.I):
            out["level"] = level
            break
    if re.search(r"緊急", q):
        out["level"] = "emergency"
    elif re.search(r"危急|嚴重", q):
        out["level"] = "critical"
    elif re.search(r"警告", q):
        out["level"] = "warning"
    sym = re.search(r"\b([A-Z]{2,10}(?:USDT)?)\b", q)
    if sym and sym.group(1) not in {"LONG", "SHORT", "PRO", "USD"}:
        out["symbol"] = sym.group(1)
    return out
