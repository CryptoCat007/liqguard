"""
Meta AI LLM client (OpenAI-compatible API) with a deterministic fallback.

Every number Muse sees is computed elsewhere; Muse only phrases it. Each
explanation exists twice: a deterministic text available instantly, and
Muse's text, fetched under a time budget, validated, and cached per input.
Callers that must not wait use the `*_now` methods: they get the cached
Muse text or the deterministic one immediately, plus a task that resolves
to Muse's answer (or None) so the message can be updated later.
"""

import asyncio
import base64
import dataclasses
import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, NotFoundError

from liqguard.i18n import ZH, is_zh, muse_language, t, tr
from liqguard.models import Alert, AlertSeverity, Position
from liqguard.risk.advice import ActionableAdvice, AdviceCalculator
from liqguard.risk.portfolio import PortfolioRiskOverview
from liqguard.risk.rescue import RescuePlan
from liqguard.risk.stops import StopCheck

from .rescue import RESCUE_EXPLANATION_SCHEMA, mock_rescue_explanation, validate_rescue_explanation
from .stops import STOP_EXPLANATION_SCHEMA, mock_stop_explanation, validate_stop_explanation

logger = logging.getLogger(__name__)

META_API_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"
# muse-spark-1.3 reasons before it answers (~900-1200 tokens); a small budget
# ends with finish_reason "length" and no content at all.
DEFAULT_MAX_TOKENS = 2000
RETRY_MAX_TOKENS = 6000
DEFAULT_TIMEOUT = 15.0
DEFAULT_REASONING_EFFORT = "low"
CACHE_TTL_SECONDS = 600
CACHE_SIZE = 256


def _env_number(name: str, default: float, cast=float, minimum: float = 0) -> float:
    try:
        value = cast(os.environ.get(name, "").strip() or default)
    except ValueError:
        logger.warning("Ignoring %s: not a number", name)
        return default
    return max(minimum, value)


@dataclass
class Instant:
    """
    What to show right now (cached Muse text, else the deterministic text) and,
    while Muse is still answering, a task resolving to its answer or None.
    """

    result: Any
    pending: Optional["asyncio.Future[Any]"] = None

    def map(self, fn: Callable[[Any], Any]) -> "Instant":
        """The same answer shaped by `fn` (now, and when Muse's arrives; None stays None)."""
        if self.pending is None:
            return Instant(fn(self.result))
        source = self.pending

        async def later() -> Any:
            live = await source
            return None if live is None else fn(live)

        return Instant(fn(self.result), asyncio.ensure_future(later()))


@dataclass
class _Job:
    kind: str
    key: str
    fallback: Callable[[], Any]
    fetch: Callable[[], Awaitable[Any]]
    """Muse's validated answer, or None after recording why it failed."""


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
        base_url: Optional[str] = None,
        use_mock: Optional[bool] = None,
        *,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
        http_client: Any = None,
    ):
        """
        Args:
            api_key: Meta API key (default: META_API_KEY). Never logged.
            model: default META_MODEL, else DEFAULT_MODEL.
            base_url: default META_API_BASE, else https://api.meta.ai/v1.
            use_mock: force mock mode; None means "mock when there is no key".
            max_tokens: completion budget incl. reasoning (default META_MAX_TOKENS, else 2000).
            timeout: seconds per Muse answer, retry included (default META_TIMEOUT, else 15).
            reasoning_effort: sent when the API accepts it (default META_REASONING_EFFORT,
                else "low"; "none" disables it).
            http_client: an `httpx.AsyncClient` for the SDK (tests inject a mock transport).
        """
        api_key = api_key or os.environ.get("META_API_KEY")
        self.model = model or os.environ.get("META_MODEL") or DEFAULT_MODEL
        self.base_url = (base_url or os.environ.get("META_API_BASE") or META_API_BASE_URL).rstrip("/")
        self.max_tokens = int(max_tokens or _env_number("META_MAX_TOKENS", DEFAULT_MAX_TOKENS, int, 256))
        self.timeout = float(timeout or _env_number("META_TIMEOUT", DEFAULT_TIMEOUT, float, 1))
        effort = (reasoning_effort if reasoning_effort is not None
                  else os.environ.get("META_REASONING_EFFORT", DEFAULT_REASONING_EFFORT)).strip().lower()
        self.reasoning_effort: Optional[str] = effort if effort not in ("", "none", "off") else None
        self._reasoning_accepted: Optional[bool] = None

        self.last_ok: Optional[bool] = None
        self.last_error: Optional[str] = None
        self.last_at: Optional[float] = None
        self._cache: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}

        self._use_mock = use_mock if use_mock is not None else not bool(api_key)
        if self._use_mock:
            logger.info("MetaLLMClient running in mock mode (deterministic responses)")
            self.client = None
        else:
            # The SDK's own retries would blow the time budget; failures fall back instead.
            self.client = AsyncOpenAI(api_key=api_key, base_url=self.base_url, timeout=self.timeout, max_retries=0,
                                      http_client=http_client)

    def describe(self) -> str:
        """One log line about the setup; never includes the key."""
        if self._use_mock:
            return "mock mode (no META_API_KEY)"
        return (f"live API · model {self.model} at {self.base_url} · max_tokens {self.max_tokens} · "
                f"timeout {self.timeout:g}s · reasoning_effort {self.reasoning_effort or 'off'}")

    def __repr__(self) -> str:
        return f"MetaLLMClient(model={self.model!r}, mock={self._use_mock})"

    @property
    def is_available(self) -> bool:
        """Check if the LLM client is available (includes mock mode)."""
        return True

    @property
    def is_mock(self) -> bool:
        """Check if running in mock mode."""
        return self._use_mock

    @property
    def mode(self) -> str:
        """"mock" (no key), "live" (the last Muse answer was used), "fallback" (it failed) or "pending" (none yet)."""
        if self._use_mock:
            return "mock"
        return {True: "live", False: "fallback"}.get(self.last_ok, "pending")

    def status(self) -> dict:
        return {"mode": self.mode, "model": self.model, "last_error": self.last_error,
                "reasoning_effort": self.reasoning_effort if self._reasoning_accepted is not False else None}

    # ------------------------------------------------------------------ transport

    def _succeeded(self) -> None:
        self.last_ok, self.last_error, self.last_at = True, None, time.time()

    def _failed(self, kind: str, reason: str) -> None:
        self.last_ok, self.last_error, self.last_at = False, reason, time.time()
        logger.warning("Muse %s failed (%s, model=%s); showing deterministic text", kind, reason, self.model)

    async def _create(self, kwargs: dict):
        try:
            response = await self.client.chat.completions.create(**kwargs)
        except APIStatusError as e:
            if "reasoning_effort" not in kwargs or e.status_code not in (400, 422) or "reasoning" not in str(e).lower():
                raise
            self._reasoning_accepted = False
            logger.info("Muse API does not accept reasoning_effort; continuing without it")
            kwargs = {k: v for k, v in kwargs.items() if k != "reasoning_effort"}
            return await self.client.chat.completions.create(**kwargs)
        if "reasoning_effort" in kwargs:
            self._reasoning_accepted = True
        return response

    async def _chat(self, kind: str, messages: list, *, json_mode: bool = True, temperature: float = 0.2) -> Optional[str]:
        """
        Muse's raw text, or None after recording why. Empty content or
        finish_reason "length" (reasoning used the budget) is retried once with
        a larger budget. Logs carry the call kind and model, never the key or prompt.
        """
        budget = self.max_tokens
        finish = None
        for attempt in (1, 2):
            kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": budget,
                                      "temperature": temperature}
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            if self.reasoning_effort and self._reasoning_accepted is not False:
                kwargs["reasoning_effort"] = self.reasoning_effort
            try:
                response = await self._create(kwargs)
            except NotFoundError:
                logger.error("Muse model %r not found at %s; set META_MODEL to a model GET /models lists",
                             self.model, self.base_url)
                self._failed(kind, "model_not_found")
                return None
            except APITimeoutError:
                self._failed(kind, "timeout")
                return None
            except APIConnectionError:
                self._failed(kind, "connection")
                return None
            except APIStatusError as e:
                self._failed(kind, f"http_{e.status_code}")
                return None
            choice = response.choices[0] if response.choices else None
            content = getattr(getattr(choice, "message", None), "content", None)
            finish = getattr(choice, "finish_reason", None)
            if isinstance(content, str) and content.strip() and finish != "length":
                return content
            logger.warning("Muse %s returned no usable content (finish_reason=%s, max_tokens=%d, reasoning_tokens=%s)%s",
                           kind, finish if isinstance(finish, str) else None, budget, _reasoning_tokens(response),
                           "; retrying with a larger budget" if attempt == 1 else "")
            budget = max(budget, min(budget * 2, RETRY_MAX_TOKENS))
        self._failed(kind, "length" if finish == "length" else "empty")
        return None

    async def _chat_json(self, kind: str, messages: list, temperature: float = 0.2) -> Optional[dict]:
        content = await self._chat(kind, messages, temperature=temperature)
        if content is None:
            return None
        try:
            data = json.loads(_strip_fence(content))
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self._failed(kind, "invalid_json")
            return None
        return data

    # ------------------------------------------------------------------ jobs & cache

    @staticmethod
    def _key(kind: str, language: str, payload: Any) -> str:
        blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return f"{kind}:{language}:{hashlib.sha256(blob.encode()).hexdigest()[:32]}"

    def _cached(self, key: str) -> Any:
        hit = self._cache.get(key)
        if hit is None:
            return None
        if hit[0] < time.monotonic():
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return hit[1]

    def _remember(self, key: str, value: Any) -> None:
        self._cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, value)
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)

    async def _fetch(self, job: _Job) -> Any:
        try:
            live = await asyncio.wait_for(job.fetch(), self.timeout)
        except asyncio.TimeoutError:
            self._failed(job.kind, "timeout")
            return None
        except Exception as e:
            self._failed(job.kind, type(e).__name__)
            return None
        if live is not None:
            self._succeeded()
            self._remember(job.key, live)
        return live

    def _start(self, job: _Job) -> asyncio.Task:
        """One request per input at a time: concurrent callers share the same task."""
        task = self._inflight.get(job.key)
        if task is None:
            task = asyncio.create_task(self._fetch(job))
            self._inflight[job.key] = task
            task.add_done_callback(lambda _t, key=job.key: self._inflight.pop(key, None))
        return task

    async def _run(self, job: _Job) -> Any:
        """Muse's answer within the time budget, else the deterministic one."""
        if self._use_mock:
            return job.fallback()
        hit = self._cached(job.key)
        if hit is not None:
            return hit
        live = await asyncio.shield(self._start(job))
        return job.fallback() if live is None else live

    def _now(self, job: _Job) -> Instant:
        if self._use_mock:
            return Instant(job.fallback())
        hit = self._cached(job.key)
        if hit is not None:
            return Instant(hit)
        # Shielded: a caller that gives up (e.g. a stopped demo) must not cancel the shared request.
        return Instant(job.fallback(), asyncio.shield(self._start(job)))

    def _rejected(self, kind: str, problem: str) -> None:
        self._failed(kind, f"rejected: {problem}")

    # ------------------------------------------------------------------ advice

    def _advice_job(self, position: Position, advice: ActionableAdvice, language: str,
                    distance_pct: Optional[float], severity: Optional[str]) -> _Job:
        def fallback() -> dict:
            return self._mock_explain_advice(position, advice, language, distance_pct, severity)

        async def fetch() -> Optional[dict]:
            user_content = f"""Position and pre-calculated advice:

Position:
{json.dumps(position.to_dict(), indent=2)}

Calculated Advice (use these exact numbers):
{json.dumps(advice.to_dict(), indent=2)}

Explain this advice to the user. Use the exact numbers provided - do not calculate new ones."""
            data = await self._chat_json("advice", [
                {"role": "system", "content": self._get_advice_system_prompt(language)},
                {"role": "user", "content": user_content},
            ], temperature=0.3)
            if data is None:
                return None
            if not all(isinstance(data.get(k), str) and data[k].strip() for k in ("summary", "explanation")):
                self._rejected("advice", "missing summary or explanation")
                return None
            base = fallback()
            return {**base, "summary": data["summary"].strip(), "explanation": data["explanation"].strip(), "mock": False}

        payload = [position.to_dict(), advice.to_dict(), distance_pct, severity]
        return _Job("advice", self._key("advice", language, payload), fallback, fetch)

    async def explain_risk_with_advice(
        self,
        position: Position,
        advice: ActionableAdvice,
        language: str = "en",
        distance_pct: Optional[float] = None,
        severity: Optional[str] = None,
    ) -> dict:
        """
        Generate explanation for pre-computed advice using LLM.

        Numbers come from advice (deterministic), LLM only explains. `distance_pct` and
        `severity` are the engine's (true liquidation, adaptive tiers) when known.
        Severity, urgency and the action always come from the engine, not from Muse.
        """
        return await self._run(self._advice_job(position, advice, language, distance_pct, severity))

    def explain_risk_with_advice_now(self, position: Position, advice: ActionableAdvice, language: str = "en",
                                     distance_pct: Optional[float] = None, severity: Optional[str] = None) -> Instant:
        return self._now(self._advice_job(position, advice, language, distance_pct, severity))

    def _get_advice_system_prompt(self, language: str) -> str:
        """Get system prompt for advice explanation."""
        if is_zh(language):
            return """你是加密貨幣風險分析師。根據提供的精確計算結果解釋風險和建議行動。

重要規則：
1. 使用提供的精確數字 - 絕不自行計算
2. 用繁體中文（台灣用語，zh-Hant-TW）清晰解釋
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
        language: str,
        distance_pct: Optional[float] = None,
        known_severity: Optional[str] = None,
    ) -> dict:
        """Generate deterministic mock explanation."""
        distance = position.distance_to_liquidation_pct if distance_pct is None else distance_pct

        if known_severity is None:
            known_severity = (
                "emergency" if distance <= 5 else "critical" if distance <= 10 else "warning" if distance <= 20 else "safe"
            )
        severity = known_severity
        urgency = {"emergency": "immediate", "critical": "high", "warning": "medium"}.get(severity, "none")

        if is_zh(language):
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

        async def fetch() -> Optional[dict]:
            data = await self._chat_json("screenshot", [
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
                ], temperature=0)
            if data is not None and not data.get("symbol"):
                self._rejected("screenshot", "no symbol")
                return None
            return data

        digest = hashlib.sha256(image_data).hexdigest()[:32]
        return await self._run(_Job("screenshot", f"screenshot:{digest}", lambda: None, fetch))

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

    def _question_job(self, positions: list[Position], question: str, language: str,
                      news_context: Optional[str]) -> _Job:
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

        def fallback() -> str:
            return self._mock_answer_question(assessments, question, language, news_context)

        async def fetch() -> Optional[str]:
            if is_zh(language):
                system_prompt = """你是加密貨幣交易助手。根據提供的倉位數據回答問題。
使用提供的精確數字，不要自行計算。用繁體中文（台灣用語，zh-Hant-TW）回答。"""
            else:
                system_prompt = """You are a crypto trading assistant. Answer questions based on the provided position data.
Use the exact numbers provided - do not calculate your own."""
            user_content = f"""Portfolio data (pre-calculated):
{json.dumps(assessments, indent=2)}

{f"Recent market news: {news_context}" if news_context else ""}

Question: {question}"""
            content = await self._chat("question", [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ], json_mode=False, temperature=0.3)
            return content.strip() if content else None

        payload = [assessments, question, news_context]
        return _Job("question", self._key("question", language, payload), fallback, fetch)

    @staticmethod
    def _no_positions_answer(language: str) -> str:
        if is_zh(language):
            return "您目前沒有持倉。使用 /bind 連接交易所或 /demo 進入演示模式。"
        return "You have no positions. Use /bind to connect an exchange or /demo for demo mode."

    async def answer_portfolio_question(
        self,
        positions: list[Position],
        question: str,
        language: str = "en",
        news_context: Optional[str] = None,
    ) -> str:
        """Answer a user's question about their portfolio from pre-calculated metrics."""
        if not positions:
            return self._no_positions_answer(language)
        return await self._run(self._question_job(positions, question, language, news_context))

    def answer_portfolio_question_now(self, positions: list[Position], question: str, language: str = "en",
                                      news_context: Optional[str] = None) -> Instant:
        if not positions:
            return Instant(self._no_positions_answer(language))
        return self._now(self._question_job(positions, question, language, news_context))

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

        if is_zh(language):
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

    @staticmethod
    def _focus_position(overview: PortfolioRiskOverview, focus: Optional[dict]):
        if focus:
            match = next((p for p in overview.position_summaries
                          if p.symbol == focus.get("symbol") and p.venue == focus.get("venue")), None)
            if match is not None:
                return match
        return overview.riskiest_position

    def portfolio_facts(self, overview: PortfolioRiskOverview, focus: Optional[dict] = None) -> dict:
        """
        Numbers for the summary card. `focus` is `PortfolioInsights.focus` (triage
        rank #1); when given it is the riskiest position, so the summary names the
        same position, distance and reason as triage and the alerts.
        """
        rp = self._focus_position(overview, focus)
        finite = [p.distance_to_liquidation_pct for p in overview.position_summaries]
        return {
            "risk_level": overview.overall_risk_level.value,
            "riskiest_symbol": rp.symbol if rp else None,
            "riskiest_venue": rp.venue if rp else None,
            "riskiest_distance_pct": round(rp.distance_to_liquidation_pct, 2) if rp else None,
            "min_distance_pct": round(min(finite), 2) if finite else None,
            "priority_basis": focus.get("basis") if focus and rp and rp.symbol == focus.get("symbol") else None,
            "priority_reason": focus.get("why") if focus and rp and rp.symbol == focus.get("symbol") else None,
            "recommended_action": self._portfolio_action(rp),
        }

    def mock_portfolio_summary(self, overview: PortfolioRiskOverview, language: str = "en",
                               focus: Optional[dict] = None) -> dict:
        """Deterministic summary from the same numbers (no LLM call), safe to compute every tick."""
        facts = self.portfolio_facts(overview, focus)
        rp = self._focus_position(overview, focus)
        return {**facts, "headline": self._mock_portfolio_headline(rp, language, facts["priority_basis"]), "mock": True}

    def _summary_job(self, overview: PortfolioRiskOverview, language: str, focus: Optional[dict]) -> _Job:
        facts = self.portfolio_facts(overview, focus)
        rp = self._focus_position(overview, focus)
        user_content = json.dumps({**facts, **overview.to_dict()["summary"]}, indent=2)

        def fallback() -> dict:
            return self.mock_portfolio_summary(overview, language, focus)

        async def fetch() -> Optional[dict]:
            if is_zh(language):
                system_prompt = (
                    "你是加密貨幣風險分析師。用一句繁體中文（台灣用語，最多 30 字）總結投資組合風險，"
                    "必須點名 riskiest_symbol 並使用提供的精確數字。回傳 JSON：{\"headline\": \"...\"}"
                )
            else:
                system_prompt = (
                    "You are a crypto risk analyst. Summarize portfolio risk in ONE sentence "
                    "(max 25 words). Name riskiest_symbol and use only the exact numbers given. "
                    "Return JSON: {\"headline\": \"...\"}"
                )
            data = await self._chat_json("summary", [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ])
            if data is None:
                return None
            headline = str(data.get("headline", "")).strip()
            if not headline or rp.symbol not in headline or not self._headline_numbers_ok(headline, facts):
                self._rejected("summary", "headline misses the riskiest position or quotes another number")
                return None
            return {**facts, "headline": headline, "mock": False}

        return _Job("summary", self._key("summary", language, user_content), fallback, fetch)

    async def summarize_portfolio(
        self,
        overview: PortfolioRiskOverview,
        language: str = "en",
        focus: Optional[dict] = None,
    ) -> dict:
        """
        One-line structured portfolio summary naming the riskiest position.

        The riskiest position (triage rank #1 when `focus` is given) and every
        number come from `overview`; the LLM only phrases the headline. Returns
        keys: headline, riskiest_symbol, riskiest_distance_pct, min_distance_pct,
        priority_basis, priority_reason, risk_level, recommended_action, mock.
        """
        if self._focus_position(overview, focus) is None:
            return self.mock_portfolio_summary(overview, language, focus)
        return await self._run(self._summary_job(overview, language, focus))

    def summarize_portfolio_now(self, overview: PortfolioRiskOverview, language: str = "en",
                                focus: Optional[dict] = None) -> Instant:
        if self._focus_position(overview, focus) is None:
            return Instant(self.mock_portfolio_summary(overview, language, focus))
        return self._now(self._summary_job(overview, language, focus))

    @staticmethod
    def _headline_numbers_ok(headline: str, facts: dict) -> bool:
        """A live headline that quotes a % must quote the riskiest distance, not another figure."""
        quoted = re.findall(r"(\d+(?:\.\d+)?)\s*%", headline)
        dist = facts.get("riskiest_distance_pct")
        if not quoted or dist is None:
            return True
        allowed = {f"{dist:.1f}", f"{dist:.2f}", f"{dist:.0f}"}
        target = (facts.get("recommended_action") or {}).get("target_distance_pct")
        if target is not None:
            allowed |= {f"{target:.0f}", f"{target:.1f}"}
        return all(q in allowed for q in quoted)

    @staticmethod
    def _portfolio_action(rp) -> dict:
        if rp and rp.margin_advice and rp.margin_advice.margin_to_add_usd:
            return {
                "action_type": "add_margin",
                "symbol": rp.symbol,
                "amount_usd": round(rp.margin_advice.margin_to_add_usd, 2),
                "target_distance_pct": rp.margin_advice.target_distance_pct,
            }
        return {"action_type": "none"}

    @staticmethod
    def _mock_portfolio_headline(rp, language: str, basis: Optional[str] = None) -> str:
        if rp is None:
            return "目前沒有持倉。" if is_zh(language) else "No open positions."
        dist = round(rp.distance_to_liquidation_pct, 2)
        add = rp.margin_advice.margin_to_add_usd if rp.margin_advice else 0
        target = rp.margin_advice.target_distance_pct if rp.margin_advice else None
        explained = basis and basis not in ("only position", "closest to liquidation")
        if is_zh(language):
            side = t(ZH, f"common.side_upper.{getattr(rp.side, 'value', rp.side).lower()}")
            venue = t(ZH, f"common.venue.{getattr(rp.venue, 'value', rp.venue).lower()}")
            line = f"最高風險為 {rp.symbol} {side}（{venue}），距離清算 {dist:.1f}%"
            if explained:
                line += f"（{tr(ZH, basis)}）"
            return line + (f"，增加 ${add:,.0f} 保證金可恢復 {target:.0f}% 緩衝。" if add else "。")
        line = f"Riskiest is {rp.symbol} {rp.side.upper()} on {rp.venue.title()} at {dist:.1f}% from liquidation"
        if explained:
            line += f" ({basis})"
        return line + (f" — add ${add:,.0f} margin to restore a {target:.0f}% buffer." if add else ".")

    def _rescue_job(self, plan: RescuePlan, language: str) -> _Job:
        plan_dict = plan.to_dict()

        def fallback() -> dict:
            return mock_rescue_explanation(plan, language)

        async def fetch() -> Optional[dict]:
            system_prompt = (
                "You explain liquidation rescue options to a crypto trader. All numbers are already computed; "
                "quote only dollar amounts that appear in the input, never compute new ones. Compare add_margin, "
                "reduce and hedge: what each costs (fees, realized loss, 24h funding 'at current rate'), how far it moves "
                "the liquidation price, and its risks. Recommend the plan's `recommended` option unless it does not reach "
                f"the target. Answer in {muse_language(language)}. Return JSON matching this schema: "
                + json.dumps(RESCUE_EXPLANATION_SCHEMA)
            )
            data = await self._chat_json("rescue", [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(plan_dict, indent=2)},
            ])
            if data is None:
                return None
            problem = validate_rescue_explanation(data, plan)
            if problem:
                self._rejected("rescue", problem)
                return None
            return {**data, "mock": False}

        return _Job("rescue", self._key("rescue", language, plan_dict), fallback, fetch)

    async def explain_rescue(self, plan: RescuePlan, language: str = "en") -> dict:
        """
        Structured trade-offs for a rescue plan (RESCUE_EXPLANATION_SCHEMA).
        Every number comes from `plan`; a live answer that recommends an option
        outside the plan or quotes an amount not in it is replaced by the mock.
        """
        return await self._run(self._rescue_job(plan, language))

    def explain_rescue_now(self, plan: RescuePlan, language: str = "en") -> Instant:
        return self._now(self._rescue_job(plan, language))

    def _stop_job(self, check: StopCheck, language: str) -> _Job:
        check_dict = check.to_dict()

        def fallback() -> dict:
            return mock_stop_explanation(check, language)

        async def fetch() -> Optional[dict]:
            system_prompt = (
                "You review a crypto trader's stop-loss against the liquidation price. All numbers are computed; quote "
                "only dollar amounts from the input. Say plainly what is wrong (no stop, stop beyond liquidation so it "
                "never fires, or stop too close to liquidation), then suggest exactly one of the computed `fixes` "
                "(move_stop, add_margin, reduce) and why. Prefer keeping the trader's stop with a smaller position when "
                f"that fix exists. Answer in {muse_language(language)}. Return JSON matching this schema: "
                + json.dumps(STOP_EXPLANATION_SCHEMA)
            )
            data = await self._chat_json("stop", [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(check_dict, indent=2)},
            ])
            if data is None:
                return None
            problem = validate_stop_explanation(data, check)
            if problem:
                self._rejected("stop", problem)
                return None
            return {**data, "mock": False}

        return _Job("stop", self._key("stop", language, check_dict), fallback, fetch)

    async def explain_stop_fix(self, check: StopCheck, language: str = "en") -> dict:
        """
        Structured stop-loss advice (STOP_EXPLANATION_SCHEMA). Muse picks one of
        the computed fixes; an answer with an unknown fix or an amount that is
        not in the check is replaced by the deterministic mock.
        """
        return await self._run(self._stop_job(check, language))

    def explain_stop_fix_now(self, check: StopCheck, language: str = "en") -> Instant:
        return self._now(self._stop_job(check, language))

    def _risk_job(self, position: Position, distance_pct: float, margin_to_safety: float,
                  severity: Optional[AlertSeverity], language: str) -> _Job:
        advice = AdviceCalculator.calculate_margin_to_add(position)
        if margin_to_safety and margin_to_safety > 0:
            advice = dataclasses.replace(advice, margin_to_add_usd=margin_to_safety, estimated_cost_usd=margin_to_safety)
        level = getattr(severity, "value", severity)
        job = self._advice_job(position, advice, language, distance_pct, level)
        default = AdviceCalculator.format_advice_text(advice, language)

        async def fetch() -> Optional[str]:
            result = await job.fetch()
            return result.get("explanation") if result else None

        return _Job("risk", job.key + ":text", lambda: job.fallback().get("explanation", default), fetch)

    async def explain_risk(
        self,
        position: Position,
        distance_pct: float,
        margin_to_safety: float,
        severity: Optional[AlertSeverity] = None,
        language: str = "en"
    ) -> str:
        """Generate a plain-language risk explanation."""
        return await self._run(self._risk_job(position, distance_pct, margin_to_safety, severity, language))

    def explain_risk_now(self, position: Position, distance_pct: float, margin_to_safety: float,
                         severity: Optional[AlertSeverity] = None, language: str = "en") -> Instant:
        return self._now(self._risk_job(position, distance_pct, margin_to_safety, severity, language))

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


def _strip_fence(content: str) -> str:
    """Some models wrap JSON in a Markdown code fence even in JSON mode."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text


def _reasoning_tokens(response) -> Optional[int]:
    details = getattr(getattr(response, "usage", None), "completion_tokens_details", None)
    value = getattr(details, "reasoning_tokens", None)
    return value if isinstance(value, int) else None


def get_news_stub(symbol: str) -> Optional[str]:
    """
    Stub for news context retrieval.

    In production, this would call a news API or search service.
    Returns None for now - can be implemented with Meta search grounding.
    """
    return None
