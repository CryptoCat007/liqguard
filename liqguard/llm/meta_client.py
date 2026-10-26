"""Meta AI LLM client using OpenAI SDK with mock fallback."""

import base64
import json
import logging
import os
from typing import Optional

from openai import AsyncOpenAI

from liqguard.models import Alert, AlertSeverity, Position
from liqguard.services.advice import ActionableAdvice, AdviceCalculator
from liqguard.services.portfolio import PortfolioRiskOverview

logger = logging.getLogger(__name__)

META_API_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark"


ADVICE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["safe", "warning", "critical", "emergency"]},
        "summary": {"type": "string", "description": "One-sentence summary"},
        "explanation": {"type": "string", "description": "2-3 sentence explanation in user's language"},
        "recommended_action": {
            "type": "object",
            "properties": {
                "action_type": {"type": "string", "enum": ["add_margin", "reduce_position", "close_position", "none"]},
                "reason": {"type": "string"},
            }
        },
        "urgency": {"type": "string", "enum": ["none", "low", "medium", "high", "immediate"]},
    },
    "required": ["severity", "summary", "explanation", "recommended_action", "urgency"]
}


POSITION_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string", "description": "Trading pair e.g. BTCUSDT"},
        "side": {"type": "string", "enum": ["long", "short"]},
        "size": {"type": "number"},
        "entry_price": {"type": "number"},
        "mark_price": {"type": "number"},
        "liquidation_price": {"type": "number"},
        "leverage": {"type": "number"},
        "margin": {"type": "number"},
        "unrealized_pnl": {"type": "number"},
        "venue": {"type": "string", "enum": ["binance", "hyperliquid", "okx", "bybit", "unknown"]},
        "confidence": {"type": "number", "description": "Confidence score 0-1"},
    },
    "required": ["symbol", "side", "size", "entry_price", "mark_price", "liquidation_price", "leverage"]
}


RISK_ANALYSIS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_position_risk_metrics",
            "description": "Get pre-calculated risk metrics for a position. Always call this first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Position symbol"}
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_margin_advice",
            "description": "Get exact margin amount needed to reach safety target",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "target_distance_pct": {"type": "number", "description": "Target distance from liquidation (default 30)"}
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_reduction_advice",
            "description": "Get exact position reduction needed to reach safety target",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "target_distance_pct": {"type": "number"}
                },
                "required": ["symbol"]
            }
        }
    },
]


class MetaLLMClient:
    """
    Client for Meta AI's LLM API (Muse Spark).
    
    When API key is not available, uses deterministic mock responses
    so demo and tests work without credentials.
    
    Features:
    - Tool calling for risk calculations (numbers computed deterministically)
    - Structured output for risk assessments
    - Plain-language explanations in English and Traditional Chinese
    - Image understanding for screenshot parsing
    - Mock mode for demo/testing
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: str = META_API_BASE_URL,
        use_mock: Optional[bool] = None,
    ):
        """
        Initialize Meta LLM client.
        
        Args:
            api_key: Meta API key. If not provided, reads from META_API_KEY env var.
            model: Model to use. Reads from META_MODEL env var or defaults to muse-spark.
            base_url: API base URL
            use_mock: Force mock mode. If None, auto-detects based on API key availability.
        """
        self.api_key = api_key or os.environ.get("META_API_KEY")
        self.model = model or os.environ.get("META_MODEL", DEFAULT_MODEL)
        self.base_url = base_url
        
        if use_mock is not None:
            self._use_mock = use_mock
        else:
            self._use_mock = not bool(self.api_key)
        
        if self._use_mock:
            logger.info("MetaLLMClient running in mock mode (deterministic responses)")
            self.client = None
        else:
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=base_url
            )
        
        self._position_cache: dict[str, Position] = {}
    
    @property
    def is_available(self) -> bool:
        """Check if the LLM client is available (includes mock mode)."""
        return True
    
    @property
    def is_mock(self) -> bool:
        """Check if running in mock mode."""
        return self._use_mock
    
    def _cache_positions(self, positions: list[Position]) -> None:
        """Cache positions for tool calling."""
        self._position_cache = {p.symbol: p for p in positions}
    
    def _get_cached_position(self, symbol: str) -> Optional[Position]:
        """Get cached position by symbol."""
        if symbol in self._position_cache:
            return self._position_cache[symbol]
        for key, pos in self._position_cache.items():
            if symbol.upper() in key.upper():
                return pos
        return None
    
    async def explain_risk_with_advice(
        self,
        position: Position,
        advice: ActionableAdvice,
        language: str = "en"
    ) -> dict:
        """
        Generate explanation for pre-computed advice using LLM.
        
        Numbers come from advice (deterministic), LLM only explains.
        """
        if self._use_mock:
            return self._mock_explain_advice(position, advice, language)
        
        self._cache_positions([position])
        
        system_prompt = self._get_advice_system_prompt(language)
        
        user_content = f"""Position and pre-calculated advice:

Position:
{json.dumps(position.to_dict(), indent=2)}

Calculated Advice (use these exact numbers):
{json.dumps(advice.to_dict(), indent=2)}

Explain this advice to the user. Use the exact numbers provided - do not calculate new ones."""
        
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                response_format={"type": "json_object"},
                max_tokens=400,
                temperature=0.3
            )
            
            result = json.loads(response.choices[0].message.content)
            result["advice"] = advice.to_dict()
            return result
            
        except Exception as e:
            logger.error(f"Error calling Meta API: {e}")
            return self._mock_explain_advice(position, advice, language)
    
    def _get_advice_system_prompt(self, language: str) -> str:
        """Get system prompt for advice explanation."""
        if language == "zh-TW":
            return """你是加密貨幣風險分析師。根據提供的精確計算結果解釋風險和建議行動。

重要規則：
1. 使用提供的精確數字 - 絕不自行計算
2. 用繁體中文清晰解釋
3. 保持簡潔（2-3句）
4. 使用 JSON 格式回應

回應格式：
{
    "severity": "safe|warning|critical|emergency",
    "summary": "一句話摘要",
    "explanation": "2-3句解釋",
    "recommended_action": {"action_type": "add_margin|reduce_position|none", "reason": "原因"},
    "urgency": "none|low|medium|high|immediate"
}"""
        else:
            return """You are a crypto risk analyst. Explain risk and recommended actions based on pre-calculated values.

IMPORTANT RULES:
1. Use the exact numbers provided - NEVER calculate your own
2. Explain clearly in plain English
3. Keep it concise (2-3 sentences)
4. Respond in JSON format

Response format:
{
    "severity": "safe|warning|critical|emergency",
    "summary": "One-sentence summary",
    "explanation": "2-3 sentence explanation",
    "recommended_action": {"action_type": "add_margin|reduce_position|none", "reason": "reason"},
    "urgency": "none|low|medium|high|immediate"
}"""
    
    def _mock_explain_advice(
        self,
        position: Position,
        advice: ActionableAdvice,
        language: str
    ) -> dict:
        """Generate deterministic mock explanation."""
        distance = position.distance_to_liquidation_pct
        
        if distance <= 5:
            severity = "emergency"
            urgency = "immediate"
        elif distance <= 10:
            severity = "critical"
            urgency = "high"
        elif distance <= 20:
            severity = "warning"
            urgency = "medium"
        else:
            severity = "safe"
            urgency = "none"
        
        if language == "zh-TW":
            if severity == "emergency":
                summary = f"{position.symbol} 距離清算僅 {distance:.1f}%，需立即行動"
                explanation = (
                    f"您的 {position.symbol} {position.side.value} 倉位處於極高風險。"
                    f"目前距離清算價格僅 {distance:.1f}%。"
                )
                if advice.margin_to_add_usd and advice.margin_to_add_usd > 0:
                    explanation += f"建議立即增加 ${advice.margin_to_add_usd:,.2f} 保證金。"
            elif severity == "critical":
                summary = f"{position.symbol} 風險嚴重，距離清算 {distance:.1f}%"
                explanation = (
                    f"您的 {position.symbol} 倉位風險較高。"
                    f"距離清算價格 {distance:.1f}%，市場劇烈波動可能導致清算。"
                )
            elif severity == "warning":
                summary = f"{position.symbol} 需要關注，距離清算 {distance:.1f}%"
                explanation = (
                    f"您的 {position.symbol} 倉位有一定風險。"
                    f"建議考慮增加保證金或減少倉位以提高安全距離。"
                )
            else:
                summary = f"{position.symbol} 目前安全，距離清算 {distance:.1f}%"
                explanation = f"您的 {position.symbol} 倉位目前處於安全距離。繼續監控市場狀況。"
        else:
            if severity == "emergency":
                summary = f"{position.symbol} is only {distance:.1f}% from liquidation - act now"
                explanation = (
                    f"Your {position.symbol} {position.side.value} position is at extreme risk. "
                    f"Only {distance:.1f}% from liquidation price. "
                )
                if advice.margin_to_add_usd and advice.margin_to_add_usd > 0:
                    explanation += f"Add ${advice.margin_to_add_usd:,.2f} margin immediately."
            elif severity == "critical":
                summary = f"{position.symbol} at critical risk - {distance:.1f}% from liquidation"
                explanation = (
                    f"Your {position.symbol} position is at elevated risk. "
                    f"At {distance:.1f}% from liquidation, sudden market moves could trigger liquidation."
                )
            elif severity == "warning":
                summary = f"{position.symbol} needs attention - {distance:.1f}% from liquidation"
                explanation = (
                    f"Your {position.symbol} position has moderate risk. "
                    f"Consider adding margin or reducing size to improve safety distance."
                )
            else:
                summary = f"{position.symbol} is safe at {distance:.1f}% from liquidation"
                explanation = f"Your {position.symbol} position is at a safe distance. Continue monitoring."
        
        action_type = advice.action_type.value if advice.margin_to_add_usd and advice.margin_to_add_usd > 0 else "none"
        
        return {
            "severity": severity,
            "summary": summary,
            "explanation": explanation,
            "recommended_action": {
                "action_type": action_type,
                "reason": AdviceCalculator.format_advice_text(advice, language) if action_type != "none" else ""
            },
            "urgency": urgency,
            "advice": advice.to_dict(),
            "mock": True,
        }
    
    async def parse_screenshot(
        self,
        image_data: bytes,
        image_type: str = "image/png"
    ) -> Optional[dict]:
        """
        Parse a position screenshot using image understanding.
        
        Returns structured position data for user confirmation.
        """
        if self._use_mock:
            return self._mock_parse_screenshot()
        
        image_b64 = base64.b64encode(image_data).decode()
        
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": """You are an expert at reading trading platform screenshots.
Extract position information and return JSON with these exact fields:
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
    "venue": "binance" or "hyperliquid" or "unknown",
    "confidence": 0.95
}
If you cannot determine a field, use null. Always include confidence score."""
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
                max_tokens=400
            )
            
            result = json.loads(response.choices[0].message.content)
            logger.info(f"Parsed screenshot: {result}")
            return result
            
        except Exception as e:
            logger.error(f"Error parsing screenshot: {e}")
            return None
    
    def _mock_parse_screenshot(self) -> dict:
        """Return mock parsed screenshot data for demo."""
        return {
            "symbol": "BTCUSDT",
            "side": "long",
            "size": 0.25,
            "entry_price": 67500.0,
            "mark_price": 65000.0,
            "liquidation_price": 58000.0,
            "leverage": 10,
            "margin": 1625.0,
            "unrealized_pnl": -625.0,
            "venue": "binance",
            "confidence": 0.85,
            "mock": True,
        }
    
    async def answer_portfolio_question(
        self,
        positions: list[Position],
        question: str,
        language: str = "en",
        news_context: Optional[str] = None,
    ) -> str:
        """
        Answer a user's question about their portfolio.
        
        Uses tool calling to get pre-calculated metrics.
        """
        if not positions:
            if language == "zh-TW":
                return "您目前沒有持倉。使用 /bind 連接交易所或 /demo 進入演示模式。"
            return "You have no positions. Use /bind to connect an exchange or /demo for demo mode."
        
        self._cache_positions(positions)
        
        assessments = []
        for pos in positions:
            advice = AdviceCalculator.calculate_margin_to_add(pos)
            assessments.append({
                "symbol": pos.symbol,
                "side": pos.side.value,
                "size": pos.size,
                "entry_price": pos.entry_price,
                "mark_price": pos.mark_price,
                "liquidation_price": pos.liquidation_price,
                "distance_to_liquidation_pct": round(pos.distance_to_liquidation_pct, 2),
                "unrealized_pnl": round(pos.unrealized_pnl, 2),
                "leverage": pos.leverage,
                "margin_to_add_for_safety": round(advice.margin_to_add_usd or 0, 2),
            })
        
        if self._use_mock:
            return self._mock_answer_question(assessments, question, language, news_context)
        
        if language == "zh-TW":
            system_prompt = """你是加密貨幣交易助手。根據提供的倉位數據回答問題。
使用提供的精確數字，不要自行計算。用繁體中文回答。"""
        else:
            system_prompt = """You are a crypto trading assistant. Answer questions based on the provided position data.
Use the exact numbers provided - do not calculate your own."""
        
        user_content = f"""Portfolio data (pre-calculated):
{json.dumps(assessments, indent=2)}

{f"Recent market news: {news_context}" if news_context else ""}

Question: {question}"""
        
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                max_tokens=500,
                temperature=0.3
            )
            
            return response.choices[0].message.content.strip()
            
        except Exception as e:
            logger.error(f"Error answering question: {e}")
            return self._mock_answer_question(assessments, question, language, news_context)
    
    def _mock_answer_question(
        self,
        assessments: list[dict],
        question: str,
        language: str,
        news_context: Optional[str]
    ) -> str:
        """Generate deterministic mock answer."""
        total_pnl = sum(a["unrealized_pnl"] for a in assessments)
        riskiest = max(assessments, key=lambda a: 100 - a["distance_to_liquidation_pct"]) if assessments else None
        
        question_lower = question.lower()
        
        if language == "zh-TW":
            if "風險" in question or "risk" in question_lower:
                if riskiest:
                    return (
                        f"📊 您最高風險的倉位是 {riskiest['symbol']}，"
                        f"距離清算 {riskiest['distance_to_liquidation_pct']:.1f}%。\n\n"
                        f"建議增加 ${riskiest['margin_to_add_for_safety']:,.2f} 保證金以達到安全距離。"
                    )
                return "您目前沒有高風險倉位。"
            elif "pnl" in question_lower or "損益" in question or "盈虧" in question:
                return f"📈 您的總未實現損益為 ${total_pnl:,.2f} USDT。"
            elif "建議" in question or "suggest" in question_lower or "should" in question_lower:
                if riskiest and riskiest["distance_to_liquidation_pct"] < 20:
                    return (
                        f"💡 建議關注 {riskiest['symbol']} 倉位，"
                        f"距離清算僅 {riskiest['distance_to_liquidation_pct']:.1f}%。"
                    )
                return "💡 您的倉位目前都在安全範圍內。繼續監控市場狀況。"
            else:
                summary = f"📊 您有 {len(assessments)} 個倉位，總未實現損益 ${total_pnl:,.2f}。"
                if riskiest:
                    summary += f"\n最需關注: {riskiest['symbol']} (距離清算 {riskiest['distance_to_liquidation_pct']:.1f}%)"
                return summary
        else:
            if "risk" in question_lower:
                if riskiest:
                    return (
                        f"📊 Your riskiest position is {riskiest['symbol']} at "
                        f"{riskiest['distance_to_liquidation_pct']:.1f}% from liquidation.\n\n"
                        f"Consider adding ${riskiest['margin_to_add_for_safety']:,.2f} margin for safety."
                    )
                return "You have no high-risk positions currently."
            elif "pnl" in question_lower or "profit" in question_lower or "loss" in question_lower:
                return f"📈 Your total unrealized PnL is ${total_pnl:,.2f} USDT."
            elif "suggest" in question_lower or "should" in question_lower or "recommend" in question_lower:
                if riskiest and riskiest["distance_to_liquidation_pct"] < 20:
                    return (
                        f"💡 Focus on {riskiest['symbol']} - only "
                        f"{riskiest['distance_to_liquidation_pct']:.1f}% from liquidation."
                    )
                return "💡 Your positions are all within safe ranges. Keep monitoring market conditions."
            else:
                summary = f"📊 You have {len(assessments)} position(s) with total unrealized PnL of ${total_pnl:,.2f}."
                if riskiest:
                    summary += f"\nMost attention needed: {riskiest['symbol']} ({riskiest['distance_to_liquidation_pct']:.1f}% from liq)"
                return summary
    
    async def summarize_portfolio(
        self,
        overview: PortfolioRiskOverview,
        language: str = "en",
    ) -> dict:
        """
        One-line structured portfolio summary naming the riskiest position.

        The riskiest position and every number come from `overview`; the LLM only
        phrases the headline. Returns keys: headline, riskiest_symbol, risk_level,
        min_distance_pct, recommended_action, mock.
        """
        rp = overview.riskiest_position
        facts = {
            "risk_level": overview.overall_risk_level.value,
            "riskiest_symbol": rp.symbol if rp else None,
            "min_distance_pct": round(rp.distance_to_liquidation_pct, 2) if rp else None,
            "recommended_action": self._portfolio_action(overview),
        }

        if self._use_mock or rp is None:
            return {**facts, "headline": self._mock_portfolio_headline(overview, language), "mock": True}

        if language == "zh-TW":
            system_prompt = (
                "你是加密貨幣風險分析師。用一句繁體中文（最多 30 字）總結投資組合風險，"
                "必須點名 riskiest_symbol 並使用提供的精確數字。回傳 JSON：{\"headline\": \"...\"}"
            )
        else:
            system_prompt = (
                "You are a crypto risk analyst. Summarize portfolio risk in ONE sentence "
                "(max 25 words). Name riskiest_symbol and use only the exact numbers given. "
                "Return JSON: {\"headline\": \"...\"}"
            )
        user_content = json.dumps({**facts, **overview.to_dict()["summary"]}, indent=2)

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
                max_tokens=120,
                temperature=0.2,
            )
            headline = str(json.loads(response.choices[0].message.content).get("headline", "")).strip()
            if not headline or rp.symbol not in headline:
                headline = self._mock_portfolio_headline(overview, language)
            return {**facts, "headline": headline, "mock": False}
        except Exception as e:
            logger.error(f"Error summarizing portfolio: {e}")
            return {**facts, "headline": self._mock_portfolio_headline(overview, language), "mock": True}

    @staticmethod
    def _portfolio_action(overview: PortfolioRiskOverview) -> dict:
        rp = overview.riskiest_position
        if rp and rp.margin_advice and rp.margin_advice.margin_to_add_usd:
            return {
                "action_type": "add_margin",
                "symbol": rp.symbol,
                "amount_usd": round(rp.margin_advice.margin_to_add_usd, 2),
                "target_distance_pct": rp.margin_advice.target_distance_pct,
            }
        return {"action_type": "none"}

    @staticmethod
    def _mock_portfolio_headline(overview: PortfolioRiskOverview, language: str) -> str:
        rp = overview.riskiest_position
        if rp is None:
            return "目前沒有持倉。" if language == "zh-TW" else "No open positions."
        level = overview.overall_risk_level.value.upper()
        dist = rp.distance_to_liquidation_pct
        add = rp.margin_advice.margin_to_add_usd if rp.margin_advice else 0
        if language == "zh-TW":
            line = f"{level}：最高風險為 {rp.symbol} {rp.side.upper()}，距離清算 {dist:.1f}%"
            return line + (f"，建議增加 ${add:,.0f} 保證金。" if add else "。")
        line = f"{level}: riskiest is {rp.symbol} {rp.side.upper()} at {dist:.1f}% from liquidation"
        return line + (f" — add ${add:,.0f} margin to reach safety." if add else ".")

    async def explain_risk(
        self,
        position: Position,
        distance_pct: float,
        margin_to_safety: float,
        severity: Optional[AlertSeverity] = None,
        language: str = "en"
    ) -> str:
        """Generate a plain-language risk explanation."""
        advice = AdviceCalculator.calculate_margin_to_add(position)
        result = await self.explain_risk_with_advice(position, advice, language)
        return result.get("explanation", AdviceCalculator.format_advice_text(advice, language))
    
    async def get_structured_risk_assessment(self, alert: Alert) -> dict:
        """Get structured risk assessment for an alert."""
        return {
            "severity": alert.severity.value,
            "reason": f"Position is {alert.distance_to_liquidation_pct:.1f}% from liquidation",
            "actions": alert.actions,
            "urgency": "immediate" if alert.severity == AlertSeverity.EMERGENCY else (
                "high" if alert.severity == AlertSeverity.CRITICAL else "medium"
            )
        }


def get_news_stub(symbol: str) -> Optional[str]:
    """
    Stub for news context retrieval.
    
    In production, this would call a news API or search service.
    Returns None for now - can be implemented with Meta search grounding.
    """
    return None
