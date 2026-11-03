"""
The release gate, replayed against a fake Muse that behaves like production:
it always reasons, reasoning tokens count against max_tokens (a budget spent
on reasoning ends with finish_reason "length" and no content), and latency
grows with the tokens generated. Times are scaled: one production second is
SCALE test seconds.
"""

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from liqguard.ai import MUSE, RULES, MetaLLMClient, MuseService
from liqguard.i18n import EN, ZH
from liqguard.simulation import MarketSimulation
from liqguard.web import create_app

from .conftest import until
from .test_muse_api import completion
from .test_muse_async import SmartMuse
from .test_muse_channels import _receive_until

SCALE = 0.05
GATE_SECONDS = 30
REASONING = {"minimal": 250, "low": 1900, "medium": 3000}
"""Reasoning tokens muse-spark spends on these short explanations at each effort (none sent: medium)."""
OUTPUT_TOKENS = 350
TOKENS_PER_SECOND = 200
FIRST_TOKEN_SECONDS = 1.0


class ProductionMuse(SmartMuse):
    def __init__(self):
        super().__init__()
        self.requests: list[dict] = []
        self.active = 0
        self.peak = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls += 1
        self.requests.append({k: v for k, v in body.items() if k != "messages"})
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            reasoning = REASONING[body.get("reasoning_effort") or "medium"]
            needed = reasoning + OUTPUT_TOKENS
            used = min(body["max_tokens"], needed)
            await asyncio.sleep((FIRST_TOKEN_SECONDS + used / TOKENS_PER_SECOND) * SCALE)
            if needed > body["max_tokens"]:
                return httpx.Response(200, json=completion(None, "length", reasoning=min(reasoning, body["max_tokens"])))
            system, user = body["messages"][0]["content"], body["messages"][1]["content"]
            self.prompts.append(system)
            data = self.answer(system, user)
            return httpx.Response(200, json=completion(json.dumps(data) if data is not None else "Muse answer."))
        finally:
            self.active -= 1


def production_llm(muse: ProductionMuse, **kw) -> MetaLLMClient:
    return MetaLLMClient(api_key="sk-test", use_mock=False, timeout=25 * SCALE,
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(muse)), **kw)


def opening_book() -> MarketSimulation:
    return MarketSimulation(None, open_at_warning=True, tick_interval=0)


async def fire_gate(service: MuseService) -> list[float]:
    """8 rescue texts (4 symbols × EN/zh) and 8 report buttons (rescue + stop-loss): seconds until each went live."""
    sim = opening_book()
    symbols = [p.symbol for p in sim.positions][:4]
    assert len(symbols) == 4
    plans = {s: sim.build_rescue_plan(s) for s in symbols}
    checks = {s: sim.protection(sim._position(s)).stop for s in symbols}
    instants = [service.rescue(plans[s], lang) for s in symbols for lang in (EN, ZH)]
    instants += [service.rescue(plans[s], EN) for s in symbols]
    instants += [service.explain_stop_fix_now(checks[s], EN) for s in symbols]
    assert len(instants) == 16
    start = time.monotonic()

    async def live_after(instant) -> float:
        if instant.live:
            return 0.0
        answer = await instant.pending
        return (time.monotonic() - start) / SCALE if answer is not None else float("inf")

    return await asyncio.gather(*(live_after(i) for i in instants))


class TestReleaseGate:
    async def test_16_concurrent_requests_are_live_within_30s(self):
        muse = ProductionMuse()
        service = MuseService(production_llm(muse))
        seconds = await fire_gate(service)
        live = [s for s in seconds if s <= GATE_SECONDS]
        assert len(live) / len(seconds) >= 0.9, sorted(seconds)
        # One request per plan and per check, both languages in one answer, no "length" retry.
        assert muse.calls <= 8
        assert {r["max_tokens"] for r in muse.requests} == {5000}
        assert {r["reasoning_effort"] for r in muse.requests} == {"minimal"}
        assert muse.peak <= 6 and service.mode == "live"

    async def test_the_fake_reproduces_the_gate_failure_with_the_old_settings(self):
        """2000 tokens at "low" effort and 3 slots: the first try runs out of tokens, the retry queues past 30s."""
        muse = ProductionMuse()
        llm = production_llm(muse, max_tokens=2000)
        llm.short_reasoning_effort, llm.concurrency = "low", 3
        seconds = await fire_gate(MuseService(llm))
        assert len([s for s in seconds if s <= GATE_SECONDS]) / len(seconds) < 0.9
        assert any(r["max_tokens"] > 2000 for r in muse.requests)


class TestOneAnswerPerPlan:
    async def test_en_and_zh_share_one_request(self):
        muse = SmartMuse(delay=0.05)
        llm = MetaLLMClient(api_key="sk-test", use_mock=False,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(muse)))
        plan = opening_book().build_rescue_plan("BTCUSDT")
        en, zh = await asyncio.gather(llm.explain_rescue(plan, EN), llm.explain_rescue(plan, ZH))
        assert en["source"] == zh["source"] == MUSE and muse.calls == 1
        assert '"zh-Hant"' in muse.prompts[0] and "Traditional Chinese" in muse.prompts[0]
        assert llm.explain_rescue_now(plan, "zh-TW").live

    async def test_a_rejected_language_falls_back_alone(self):
        sim = opening_book()
        plan = sim.build_rescue_plan("BTCUSDT")

        async def reply(request):
            good = {"headline": "Muse picks a plan.", "recommended_option": plan.recommended,
                    "trade_offs": [{"option": plan.recommended, "summary": "ok", "pros": [], "cons": []}], "caveat": ""}
            bad = {**good, "headline": "加 $123,456.78 就好。"}
            return httpx.Response(200, json=completion(json.dumps({"en": good, "zh-Hant": bad})))

        llm = MetaLLMClient(api_key="sk-test", use_mock=False,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(reply)))
        en = await llm.explain_rescue(plan, EN)
        zh = await llm.explain_rescue(plan, ZH)
        assert en["source"] == MUSE and en["headline"] == "Muse picks a plan."
        assert zh["source"] == RULES and "123,456" not in zh["headline"]
        assert llm.mode == "live" and "zh-Hant" in llm.last_error

    async def test_same_position_across_surfaces_asks_once(self):
        muse = SmartMuse(delay=0.05)
        service = MuseService(MetaLLMClient(api_key="sk-test", use_mock=False,
                                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(muse))))
        sim = opening_book()
        plan, check = sim.build_rescue_plan("BTCUSDT"), sim.protection(sim._position("BTCUSDT")).stop
        notes = [service.tool_note("rescue", EN, plan=plan), service.tool_note("rescue", ZH, plan=plan),
                 service.tool_note("stop", ZH, check=check), service.tool_note("stop", EN, check=check)]
        later = await asyncio.gather(service.rescue(plan, ZH).pending, *(n.pending for n in notes))
        assert all(x is not None for x in later) and muse.calls == 2


class TestBoundedWarmUp:
    async def test_warm_ups_take_at_most_half_the_slots_and_never_repeat_quickly(self):
        gate = asyncio.Event()
        muse = SmartMuse(gate)
        llm = MetaLLMClient(api_key="sk-test", use_mock=False,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(muse)))
        sims = [MarketSimulation(None, scenario=s, open_at_warning=True, tick_interval=0) for s in ("mixed", "cross_longs")]
        checks = [sim.protection(p).stop for sim in sims for p in sim.positions]
        checks = [c for c in checks if c.needs_warning]
        assert len(checks) > llm.concurrency // 2
        service = MuseService(llm)
        service.warm(EN, checks=tuple(checks))
        assert await until(lambda: muse.calls == llm.concurrency // 2)
        await asyncio.sleep(0.05)
        assert muse.calls == llm.concurrency // 2
        gate.set()
        await asyncio.sleep(0.1)
        service.warm(EN, checks=tuple(checks[:3]))
        await asyncio.sleep(0.05)
        assert muse.calls == llm.concurrency // 2

    async def test_public_demo_is_precomputed_at_startup_for_both_languages(self):
        muse = SmartMuse()
        llm = MetaLLMClient(api_key="sk-test", use_mock=False,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(muse)))
        with TestClient(create_app(llm_client=llm), base_url="https://testserver") as c:
            assert await until(lambda: muse.calls >= 2 and not llm._inflight, timeout=10)
            asked = muse.calls
            for lang in ("en", "zh-Hant"):
                body = c.get(f"/api/demo/rescue?symbol=BTCUSDT&lang={lang}").json()
                assert body["muse_pending"] is False and body["explanation"]["source"] == MUSE
                report = c.get(f"/api/demo/position-tool?symbol=BTCUSDT&tool=stop&lang={lang}").json()
                assert report["muse_pending"] is False and report["muse"]["source"] == MUSE
            assert muse.calls == asked and c.get("/api/health").json()["muse_mode"] == "live"


class TestWebSocketEventNames:
    """What a release script must listen for on /ws/demo after a dashboard request returns `muse_pending`."""

    @pytest.mark.parametrize("healthy", [True, False])
    def test_rescue_and_report_buttons_always_end_with_their_event(self, healthy):
        async def broken(request):
            return httpx.Response(503, json={"error": {"message": "unavailable"}})

        muse = SmartMuse(delay=0.2)
        llm = MetaLLMClient(api_key="sk-test", use_mock=False,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(muse if healthy else broken)))
        sim = MarketSimulation(llm, open_at_warning=True, tick_interval=0)
        with TestClient(create_app(demo_simulation=sim), base_url="https://testserver") as c:
            with c.websocket_connect("/ws/demo") as ws:
                assert ws.receive_json()["type"] == "snapshot"
                symbol = next(p.symbol for p in sim.positions if sim.protection(p).stop.needs_warning)
                rescue = c.get(f"/api/demo/rescue?symbol={symbol}").json()
                report = c.get(f"/api/demo/position-tool?symbol={symbol}&tool=stop").json()
                expected = {k for k, pending in (("rescue_muse", rescue["muse_pending"]),
                                                 ("report_muse", report["muse_pending"])) if pending}
                seen = _receive_until(ws, expected)
        assert expected <= seen.keys(), seen.keys()
        assert expected, "the public demo should open on a position Muse has something to say about"
        for event in (seen[k] for k in expected):
            assert event["data"]["symbol"] == symbol and event["data"]["scope"] == "sim"
            assert event["data"].get("muse_failed", False) is not healthy
        if "rescue_muse" in seen:
            assert seen["rescue_muse"]["data"]["explanation"]["source"] == (MUSE if healthy else RULES)
        if "report_muse" in seen:
            assert seen["report_muse"]["data"]["report"]["tool"] == "stop"
