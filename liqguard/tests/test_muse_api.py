"""
The live Muse path against a mocked HTTP layer: budgets, retries, 404s,
timeouts, the reasoning-effort fallback, caching and the honest status.
"""

import asyncio
import json
import logging

import httpx
import pytest

from liqguard.ai import MetaLLMClient
from liqguard.ai.muse import DEFAULT_MAX_TOKENS, DEFAULT_MODEL
from liqguard.models import Position, Side, Venue
from liqguard.risk.advice import AdviceCalculator
from liqguard.risk.portfolio import PortfolioAnalyzer

KEY = "sk-muse-test-secret-0123456789"
HEADLINE = "BTCUSDT is closest to liquidation."


def completion(content, finish="stop", reasoning=1000) -> dict:
    return {
        "id": "c1", "object": "chat.completion", "created": 0, "model": DEFAULT_MODEL,
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": reasoning + 20, "total_tokens": reasoning + 70,
                  "completion_tokens_details": {"reasoning_tokens": reasoning}},
    }


class FakeMuse:
    """Records request bodies and answers from a script of (status, body | callable)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.urls: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.urls.append(str(request.url))
        status, body = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if callable(body):
            body = await body()
        return httpx.Response(status, json=body)


def client_for(fake: FakeMuse, **kwargs) -> MetaLLMClient:
    return MetaLLMClient(api_key=KEY, use_mock=False, http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
                         **kwargs)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("META_MODEL", "META_API_BASE", "META_MAX_TOKENS", "META_TIMEOUT", "META_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def overview():
    positions = [
        Position(Venue.BINANCE, "BTCUSDT", Side.LONG, 0.5, 65000, 60000, 57500, 3000, 10, -2500),
        Position(Venue.HYPERLIQUID, "ETH", Side.SHORT, 5, 3200, 3100, 4000, 4000, 4, 500),
    ]
    return PortfolioAnalyzer().analyze_portfolio(positions)


def ok(headline=HEADLINE):
    return 200, completion(json.dumps({"headline": headline}))


class TestRequest:
    async def test_defaults_model_budget_base_and_reasoning(self, overview):
        fake = FakeMuse(ok())
        llm = client_for(fake)
        assert llm.mode == "pending"
        result = await llm.summarize_portfolio(overview)
        assert result["headline"] == HEADLINE and result["mock"] is False
        body = fake.requests[0]
        assert body["model"] == "muse-spark-1.3" and body["max_tokens"] == DEFAULT_MAX_TOKENS == 2000
        assert body["reasoning_effort"] == "low" and body["response_format"] == {"type": "json_object"}
        assert fake.urls[0] == "https://api.meta.ai/v1/chat/completions"
        assert llm.mode == "live" and llm.status()["reasoning_effort"] == "low"

    async def test_env_overrides(self, overview, monkeypatch):
        monkeypatch.setenv("META_MODEL", "muse-spark-1.2")
        monkeypatch.setenv("META_API_BASE", "https://muse.example.test/v2/")
        monkeypatch.setenv("META_MAX_TOKENS", "3000")
        monkeypatch.setenv("META_REASONING_EFFORT", "none")
        fake = FakeMuse(ok())
        await client_for(fake).summarize_portfolio(overview)
        body = fake.requests[0]
        assert body["model"] == "muse-spark-1.2" and body["max_tokens"] == 3000 and "reasoning_effort" not in body
        assert fake.urls[0] == "https://muse.example.test/v2/chat/completions"

    async def test_rejected_reasoning_effort_is_dropped_for_good(self, overview, caplog):
        error = {"error": {"message": "Unrecognized request argument supplied: reasoning_effort", "type": "invalid_request_error"}}
        fake = FakeMuse((400, error), ok(), ok("BTCUSDT still leads the risk."))
        llm = client_for(fake)
        assert (await llm.summarize_portfolio(overview))["mock"] is False
        assert "reasoning_effort" in fake.requests[0] and "reasoning_effort" not in fake.requests[1]
        overview.position_summaries[0].distance_to_liquidation_pct += 0.01
        await llm.summarize_portfolio(overview)
        assert len(fake.requests) == 3 and "reasoning_effort" not in fake.requests[2]
        assert llm.mode == "live" and llm.status()["reasoning_effort"] is None

    async def test_other_400_is_a_clean_failure(self, overview):
        fake = FakeMuse((400, {"error": {"message": "bad messages"}}))
        llm = client_for(fake)
        result = await llm.summarize_portfolio(overview)
        assert result["mock"] is True and len(fake.requests) == 1
        assert llm.mode == "fallback" and llm.last_error == "http_400"


class TestFailures:
    async def test_null_content_is_retried_with_a_larger_budget(self, overview):
        fake = FakeMuse((200, completion(None, "length", reasoning=2000)), ok())
        llm = client_for(fake)
        result = await llm.summarize_portfolio(overview)
        assert result["headline"] == HEADLINE and result["mock"] is False
        assert [r["max_tokens"] for r in fake.requests] == [2000, 4000]
        assert llm.mode == "live"

    async def test_null_content_twice_falls_back_to_deterministic_text(self, overview, caplog):
        fake = FakeMuse((200, completion(None, "length", reasoning=4000)))
        llm = client_for(fake)
        with caplog.at_level(logging.WARNING):
            result = await llm.summarize_portfolio(overview)
        assert result == llm.mock_portfolio_summary(overview)
        assert len(fake.requests) == 2 and llm.mode == "fallback" and llm.last_error == "length"
        assert "finish_reason=length" in caplog.text and "reasoning_tokens=4000" in caplog.text

    async def test_truncated_content_counts_as_a_failure(self, overview):
        fake = FakeMuse((200, completion('{"headline": "BTCUSDT is', "length")))
        llm = client_for(fake)
        assert (await llm.summarize_portfolio(overview))["mock"] is True
        assert llm.last_error == "length"

    async def test_model_not_found(self, overview, caplog):
        fake = FakeMuse((404, {"error": {"message": "The model `muse-spark` does not exist", "code": "model_not_found"}}))
        llm = client_for(fake, model="muse-spark")
        with caplog.at_level(logging.WARNING):
            result = await llm.summarize_portfolio(overview)
        assert result["mock"] is True and len(fake.requests) == 1
        assert llm.mode == "fallback" and llm.last_error == "model_not_found"
        assert "META_MODEL" in caplog.text

    async def test_timeout(self, overview):
        async def slow():
            await asyncio.sleep(5)
            return completion(json.dumps({"headline": HEADLINE}))

        llm = client_for(FakeMuse((200, slow)), timeout=0.2)
        started = asyncio.get_running_loop().time()
        result = await llm.summarize_portfolio(overview)
        assert asyncio.get_running_loop().time() - started < 2
        assert result["mock"] is True and llm.mode == "fallback" and llm.last_error == "timeout"

    async def test_invalid_json(self, overview):
        llm = client_for(FakeMuse((200, completion("I think BTCUSDT is risky."))))
        assert (await llm.summarize_portfolio(overview))["mock"] is True
        assert llm.last_error == "invalid_json"

    async def test_fenced_json_is_accepted(self, overview):
        fenced = "```json\n" + json.dumps({"headline": HEADLINE}) + "\n```"
        llm = client_for(FakeMuse((200, completion(fenced))))
        assert (await llm.summarize_portfolio(overview))["headline"] == HEADLINE

    async def test_logs_never_carry_the_key_or_the_prompt(self, overview, caplog):
        fake = FakeMuse((200, completion(None, "length")), (404, {"error": {"message": "nope"}}), (500, {"error": {}}))
        llm = client_for(fake)
        with caplog.at_level(logging.DEBUG):
            for _ in range(3):
                await llm.summarize_portfolio(overview)
                overview.position_summaries[0].distance_to_liquidation_pct += 0.01
        assert KEY not in caplog.text and "Summarize portfolio risk" not in caplog.text
        assert "riskiest_distance_pct" not in caplog.text


class TestStatus:
    async def test_badge_follows_the_last_call(self, overview):
        fake = FakeMuse(ok(), (500, {"error": {"message": "boom"}}), ok("BTCUSDT is back in focus."))
        llm = client_for(fake)
        await llm.summarize_portfolio(overview)
        assert llm.mode == "live"
        overview.position_summaries[0].distance_to_liquidation_pct += 0.01
        await llm.summarize_portfolio(overview)
        assert llm.mode == "fallback" and llm.last_error == "http_500"
        overview.position_summaries[0].distance_to_liquidation_pct += 0.01
        await llm.summarize_portfolio(overview)
        assert llm.mode == "live" and llm.last_error is None

    async def test_rejected_answer_is_not_live(self, overview):
        llm = client_for(FakeMuse(ok("ETH looks risky")))
        assert (await llm.summarize_portfolio(overview))["mock"] is True
        assert llm.mode == "fallback"

    def test_mock_mode(self):
        assert MetaLLMClient(use_mock=True).mode == "mock"


class TestCacheAndInstant:
    async def test_same_snapshot_is_asked_once(self, overview):
        fake = FakeMuse(ok())
        llm = client_for(fake)
        first, second = await asyncio.gather(llm.summarize_portfolio(overview), llm.summarize_portfolio(overview))
        third = await llm.summarize_portfolio(overview)
        assert first == second == third and len(fake.requests) == 1
        overview.position_summaries[0].distance_to_liquidation_pct += 0.5
        await llm.summarize_portfolio(overview)
        assert len(fake.requests) == 2

    async def test_language_is_part_of_the_key(self, overview):
        fake = FakeMuse(ok())
        llm = client_for(fake)
        await llm.summarize_portfolio(overview, "en")
        await llm.summarize_portfolio(overview, "zh-Hant")
        assert len(fake.requests) == 2

    async def test_failures_are_not_cached(self, overview):
        fake = FakeMuse((500, {"error": {}}), ok())
        llm = client_for(fake)
        assert (await llm.summarize_portfolio(overview))["mock"] is True
        assert (await llm.summarize_portfolio(overview))["mock"] is False

    async def test_now_returns_deterministic_text_then_muse(self, overview):
        gate = asyncio.Event()

        async def gated():
            await gate.wait()
            return completion(json.dumps({"headline": HEADLINE}))

        llm = client_for(FakeMuse((200, gated)))
        instant = llm.summarize_portfolio_now(overview)
        assert instant.result == llm.mock_portfolio_summary(overview) and instant.pending is not None
        gate.set()
        live = await instant.pending
        assert live["headline"] == HEADLINE
        again = llm.summarize_portfolio_now(overview)
        assert again.result == live and again.pending is None

    async def test_now_pending_resolves_to_none_on_failure(self, overview):
        llm = client_for(FakeMuse((404, {"error": {}})))
        instant = llm.summarize_portfolio_now(overview)
        assert instant.result["mock"] is True and await instant.pending is None

    def test_now_in_mock_mode_has_nothing_pending(self, overview):
        instant = MetaLLMClient(use_mock=True).summarize_portfolio_now(overview)
        assert instant.result["mock"] is True and instant.pending is None

    async def test_explain_risk_now_maps_to_text(self, overview):
        reply = {"summary": "BTCUSDT needs margin.", "explanation": "Add margin to BTCUSDT now.", "severity": "safe"}
        llm = client_for(FakeMuse((200, completion(json.dumps(reply)))))
        pos = Position(Venue.BINANCE, "BTCUSDT", Side.LONG, 0.5, 65000, 60000, 57500, 3000, 10, -2500)
        instant = llm.explain_risk_now(pos, 4.2, 900.0, None, "en")
        assert isinstance(instant.result, str) and instant.result != reply["explanation"]
        assert await instant.pending == reply["explanation"]

    async def test_engine_keeps_severity_when_muse_phrases_advice(self):
        reply = {"summary": "Fine.", "explanation": "Relax.", "severity": "safe", "urgency": "none"}
        llm = client_for(FakeMuse((200, completion(json.dumps(reply)))))
        pos = Position(Venue.BINANCE, "BTCUSDT", Side.LONG, 0.5, 65000, 60000, 57500, 3000, 10, -2500)
        result = await llm.explain_risk_with_advice(pos, AdviceCalculator.calculate_margin_to_add(pos), "en", 4.2, "emergency")
        assert result["mock"] is False and result["explanation"] == "Relax."
        assert result["severity"] == "emergency" and result["urgency"] == "immediate"
