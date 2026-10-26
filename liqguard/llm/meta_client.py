"""Meta AI LLM client using OpenAI SDK."""

import json
import logging
import os
from typing import Optional

from openai import AsyncOpenAI

from liqguard.models import Alert, AlertSeverity, Position

logger = logging.getLogger(__name__)

META_API_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark"


RISK_ANALYSIS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculate_liquidation_distance",
            "description": "Calculate the distance from current mark price to liquidation price as a percentage",
            "parameters": {
                "type": "object",
                "properties": {
                    "mark_price": {
                        "type": "number",
                        "description": "Current mark price of the asset"
                    },
                    "liquidation_price": {
                        "type": "number",
                        "description": "Price at which position will be liquidated"
                    },
                    "side": {
                        "type": "string",
                        "enum": ["long", "short"],
                        "description": "Position side"
                    }
                },
                "required": ["mark_price", "liquidation_price", "side"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_margin_needed",
            "description": "Calculate additional margin needed to reach a safe distance from liquidation",
            "parameters": {
                "type": "object",
                "properties": {
                    "current_distance_pct": {
                        "type": "number",
                        "description": "Current distance to liquidation in percentage"
                    },
                    "target_distance_pct": {
                        "type": "number",
                        "description": "Target safe distance in percentage"
                    },
                    "position_size": {
                        "type": "number",
                        "description": "Size of the position"
                    },
                    "mark_price": {
                        "type": "number",
                        "description": "Current mark price"
                    },
                    "leverage": {
                        "type": "number",
                        "description": "Position leverage"
                    }
                },
                "required": ["current_distance_pct", "target_distance_pct", "position_size", "mark_price", "leverage"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_risk_level",
            "description": "Determine the risk level based on distance to liquidation",
            "parameters": {
                "type": "object",
                "properties": {
                    "distance_pct": {
                        "type": "number",
                        "description": "Distance to liquidation as percentage"
                    }
                },
                "required": ["distance_pct"]
            }
        }
    }
]


class MetaLLMClient:
    """
    Client for Meta AI's LLM API (Muse Spark).

    Features:
    - Tool calling for risk calculations
    - Structured output for risk assessments
    - Plain-language explanations in English and Traditional Chinese
    - Image understanding for screenshot parsing
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        base_url: str = META_API_BASE_URL
    ):
        """
        Initialize Meta LLM client.

        Args:
            api_key: Meta API key. If not provided, reads from META_API_KEY env var.
            model: Model to use (default: muse-spark)
            base_url: API base URL
        """
        self.api_key = api_key or os.environ.get("META_API_KEY")
        if not self.api_key:
            logger.warning("No META_API_KEY provided. LLM features will be disabled.")

        self.model = model
        self.base_url = base_url

        self.client = AsyncOpenAI(
            api_key=self.api_key or "dummy",
            base_url=base_url
        ) if self.api_key else None

    @property
    def is_available(self) -> bool:
        """Check if the LLM client is available."""
        return self.client is not None and bool(self.api_key)

    async def explain_risk(
        self,
        position: Position,
        distance_pct: float,
        margin_to_safety: float,
        severity: Optional[AlertSeverity] = None,
        language: str = "en"
    ) -> str:
        """
        Generate a plain-language risk explanation.

        Args:
            position: Position to explain
            distance_pct: Distance to liquidation percentage
            margin_to_safety: Additional margin needed
            severity: Alert severity level
            language: Language code ('en' or 'zh-TW')

        Returns:
            Human-readable risk explanation
        """
        if not self.is_available:
            return self._fallback_explanation(
                position, distance_pct, margin_to_safety, severity, language
            )

        severity_str = severity.value if severity else "safe"

        if language == "zh-TW":
            system_prompt = """你是一位加密貨幣風險分析師。請用繁體中文簡潔地解釋倉位風險。
使用清晰的語言，避免技術術語。包含：
1. 目前風險等級和含義
2. 需要採取的具體行動
3. 如果不採取行動可能發生什麼"""
        else:
            system_prompt = """You are a crypto risk analyst. Explain position risks concisely.
Use clear language, avoid jargon. Include:
1. Current risk level and what it means
2. Specific actions to take
3. What could happen if no action is taken"""

        user_prompt = f"""Analyze this perpetual futures position:

Symbol: {position.symbol}
Side: {position.side.value}
Size: {position.size}
Entry Price: ${position.entry_price:,.2f}
Current Mark Price: ${position.mark_price:,.2f}
Liquidation Price: ${position.liquidation_price:,.2f}
Leverage: {position.leverage}x
Unrealized PnL: ${position.unrealized_pnl:,.2f}

Risk Metrics:
- Distance to Liquidation: {distance_pct:.2f}%
- Risk Level: {severity_str.upper()}
- Additional Margin Needed for Safety: ${margin_to_safety:,.2f}

Provide a brief (2-3 sentences) risk assessment and recommended action."""

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=300,
                temperature=0.3
            )

            return response.choices[0].message.content.strip()

        except Exception as e:
            logger.error(f"Error calling Meta API: {e}")
            return self._fallback_explanation(
                position, distance_pct, margin_to_safety, severity, language
            )

    def _fallback_explanation(
        self,
        position: Position,
        distance_pct: float,
        margin_to_safety: float,
        severity: Optional[AlertSeverity],
        language: str
    ) -> str:
        """Generate fallback explanation when API is unavailable."""
        if language == "zh-TW":
            if severity == AlertSeverity.EMERGENCY:
                return (
                    f"緊急警告！您的 {position.symbol} {position.side.value} 倉位距離清算僅 {distance_pct:.1f}%。"
                    f"建議立即增加 ${margin_to_safety:,.2f} 保證金或減少倉位。"
                )
            elif severity == AlertSeverity.CRITICAL:
                return (
                    f"嚴重風險：{position.symbol} 倉位距離清算 {distance_pct:.1f}%。"
                    f"請考慮增加 ${margin_to_safety:,.2f} 保證金以恢復安全距離。"
                )
            elif severity == AlertSeverity.WARNING:
                return (
                    f"風險警告：{position.symbol} 倉位距離清算 {distance_pct:.1f}%。"
                    f"市場波動可能導致清算。建議增加保證金或設置止損。"
                )
            else:
                return f"{position.symbol} 倉位目前安全，距離清算 {distance_pct:.1f}%。"
        else:
            if severity == AlertSeverity.EMERGENCY:
                return (
                    f"EMERGENCY: Your {position.symbol} {position.side.value} position is only {distance_pct:.1f}% "
                    f"from liquidation! Add ${margin_to_safety:,.2f} margin or reduce position immediately."
                )
            elif severity == AlertSeverity.CRITICAL:
                return (
                    f"CRITICAL: {position.symbol} position is {distance_pct:.1f}% from liquidation. "
                    f"Consider adding ${margin_to_safety:,.2f} margin to restore safety."
                )
            elif severity == AlertSeverity.WARNING:
                return (
                    f"WARNING: {position.symbol} position is {distance_pct:.1f}% from liquidation. "
                    f"Market volatility could trigger liquidation. Consider adding margin or setting a stop-loss."
                )
            else:
                return f"{position.symbol} position is currently safe at {distance_pct:.1f}% from liquidation."

    async def analyze_with_tools(
        self,
        position: Position,
        user_question: Optional[str] = None
    ) -> dict:
        """
        Analyze position using tool calling for calculations.

        Args:
            position: Position to analyze
            user_question: Optional user question about the position

        Returns:
            Structured analysis result
        """
        if not self.is_available:
            return {
                "analysis": self._fallback_explanation(
                    position,
                    position.distance_to_liquidation_pct,
                    position.margin_to_reach_distance(30),
                    None,
                    "en"
                ),
                "tools_used": [],
                "calculations": {}
            }

        messages = [
            {
                "role": "system",
                "content": """You are a crypto risk analyst with access to calculation tools.
Use the tools to perform precise calculations, then explain the results clearly.
Always use the tools for numerical analysis rather than estimating."""
            },
            {
                "role": "user",
                "content": f"""Analyze this position and use tools to calculate the risk:

{json.dumps(position.to_dict(), indent=2)}

{user_question or "What is the current risk level and what actions should I take?"}"""
            }
        ]

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=RISK_ANALYSIS_TOOLS,
                tool_choice="auto",
                max_tokens=500
            )

            message = response.choices[0].message
            tools_used = []
            calculations = {}

            if message.tool_calls:
                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = json.loads(tool_call.function.arguments)
                    tools_used.append(tool_name)

                    if tool_name == "calculate_liquidation_distance":
                        result = position.distance_to_liquidation_pct
                    elif tool_name == "calculate_margin_needed":
                        result = position.margin_to_reach_distance(
                            tool_args.get("target_distance_pct", 30)
                        )
                    elif tool_name == "get_risk_level":
                        dist = tool_args.get("distance_pct", position.distance_to_liquidation_pct)
                        if dist <= 5:
                            result = "EMERGENCY"
                        elif dist <= 10:
                            result = "CRITICAL"
                        elif dist <= 20:
                            result = "WARNING"
                        else:
                            result = "SAFE"
                    else:
                        result = "unknown"

                    calculations[tool_name] = {
                        "args": tool_args,
                        "result": result
                    }

            return {
                "analysis": message.content or self._fallback_explanation(
                    position,
                    position.distance_to_liquidation_pct,
                    position.margin_to_reach_distance(30),
                    None,
                    "en"
                ),
                "tools_used": tools_used,
                "calculations": calculations
            }

        except Exception as e:
            logger.error(f"Error in tool-calling analysis: {e}")
            return {
                "analysis": self._fallback_explanation(
                    position,
                    position.distance_to_liquidation_pct,
                    position.margin_to_reach_distance(30),
                    None,
                    "en"
                ),
                "tools_used": [],
                "calculations": {},
                "error": str(e)
            }

    async def get_structured_risk_assessment(
        self,
        alert: Alert
    ) -> dict:
        """
        Get structured risk assessment for an alert.

        Returns JSON with severity, reason, and actions.
        """
        if not self.is_available:
            return {
                "severity": alert.severity.value,
                "reason": f"Position is {alert.distance_to_liquidation_pct:.1f}% from liquidation",
                "actions": alert.actions,
                "urgency": "high" if alert.severity in [AlertSeverity.CRITICAL, AlertSeverity.EMERGENCY] else "medium"
            }

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": """You are a risk assessment system. Output JSON only.
Required format:
{
    "severity": "warning|critical|emergency",
    "reason": "brief explanation",
    "actions": ["action1", "action2"],
    "urgency": "low|medium|high|immediate"
}"""
                    },
                    {
                        "role": "user",
                        "content": f"Assess this alert: {json.dumps(alert.to_dict())}"
                    }
                ],
                response_format={"type": "json_object"},
                max_tokens=200
            )

            return json.loads(response.choices[0].message.content)

        except Exception as e:
            logger.error(f"Error getting structured assessment: {e}")
            return {
                "severity": alert.severity.value,
                "reason": f"Position is {alert.distance_to_liquidation_pct:.1f}% from liquidation",
                "actions": alert.actions,
                "urgency": "high" if alert.severity in [AlertSeverity.CRITICAL, AlertSeverity.EMERGENCY] else "medium"
            }

    async def parse_screenshot(
        self,
        image_data: bytes,
        image_type: str = "image/png"
    ) -> Optional[dict]:
        """
        Parse a position screenshot using image understanding.

        Args:
            image_data: Raw image bytes
            image_type: MIME type of the image

        Returns:
            Parsed position data or None if parsing failed
        """
        if not self.is_available:
            logger.warning("LLM not available for screenshot parsing")
            return None

        import base64
        image_b64 = base64.b64encode(image_data).decode()

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": """You are an expert at reading trading platform screenshots.
Extract position information and return JSON with these fields:
{
    "symbol": "BTCUSDT",
    "side": "long" or "short",
    "size": 0.5,
    "entry_price": 65000.0,
    "mark_price": 64500.0,
    "liquidation_price": 60000.0,
    "leverage": 10,
    "margin": 3250.0,
    "unrealized_pnl": -250.0,
    "venue": "binance" or "hyperliquid" or "unknown"
}
If you cannot determine a field, use null. Only return the JSON object."""
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Extract the position information from this trading screenshot:"
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{image_type};base64,{image_b64}"
                                }
                            }
                        ]
                    }
                ],
                response_format={"type": "json_object"},
                max_tokens=300
            )

            result = json.loads(response.choices[0].message.content)
            logger.info(f"Parsed screenshot: {result}")
            return result

        except Exception as e:
            logger.error(f"Error parsing screenshot: {e}")
            return None

    async def answer_portfolio_question(
        self,
        positions: list[Position],
        question: str,
        language: str = "en"
    ) -> str:
        """
        Answer a user's question about their portfolio.

        Args:
            positions: List of current positions
            question: User's question
            language: Response language

        Returns:
            Answer to the question
        """
        if not self.is_available:
            if language == "zh-TW":
                return "LLM 服務暫時不可用。請稍後再試。"
            return "LLM service is temporarily unavailable. Please try again later."

        positions_summary = [p.to_dict() for p in positions]

        if language == "zh-TW":
            system_prompt = "你是一位加密貨幣交易助手。請用繁體中文回答關於用戶投資組合的問題。"
        else:
            system_prompt = "You are a crypto trading assistant. Answer questions about the user's portfolio."

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": f"Portfolio:\n{json.dumps(positions_summary, indent=2)}\n\nQuestion: {question}"
                    }
                ],
                max_tokens=400
            )

            return response.choices[0].message.content.strip()

        except Exception as e:
            logger.error(f"Error answering question: {e}")
            if language == "zh-TW":
                return f"抱歉，處理您的問題時發生錯誤：{str(e)}"
            return f"Sorry, an error occurred while processing your question: {str(e)}"
