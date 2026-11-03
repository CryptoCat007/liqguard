"""
Live Muse under load: a burst of requests queues instead of timing out, the
same position asks once, answers that round a plan figure are accepted,
prompts carry figures written for a reader, and "(mock)" only ever means
"no key is configured".
"""

import asyncio
import dataclasses
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from liqguard.ai import (
    MOCK,
    MUSE,
    RULES,
    MetaLLMClient,
    MuseService,
    mock_rescue_explanation,
    validate_rescue_explanation,
)
from liqguard.ai.figures import pct, signed_usd, usd
from liqguard.ai.muse import raw_figure
from liqguard.alerts import format_stop_message, recommendation_line
from liqguard.i18n import EN, ZH, t
from liqguard.simulation import MarketSimulation
from liqguard.telegram.sim_bridge import TelegramSimulationBridge

from .conftest import until
from .test_muse_api import completion
from .test_muse_async import SmartMuse, live_llm


def demo(llm, **kw) -> MarketSimulation:
    return MarketSimulation(llm, tick_interval=0, escalation_seconds_per_minute=0.01, **kw)


@pytest.fixture
def plan():
    return demo(None).build_rescue_plan("BTCUSDT")


class TestFigures:
    def test_reader_formats(self):
        assert usd(-7674.0) == "-$7,674" and signed_usd(-7674.0) == "-$7,674" and signed_usd(1204.4) == "+$1,204"
        assert usd(1381.2234, cents=True) == "$1,381.22" and usd(12.5) == "$12.50"
        assert pct(5.153) == "5.2%" and pct(30, 0) == "30%"

    @pytest.mark.parametrize("text, raw", [
        ("SOLUSDT riskiest at 5.15 distance", True),
        ("Book at -7674.0 PnL", True),
        ("SOLUSDT is 5.2% from liquidation; PnL -$7,674", False),
        ("Add $1,381.22 margin at 2.5x", False),
        ("closest at ≈ 1.8 typical 5m moves", False),
    ])
    def test_raw_decimals_are_spotted(self, text, raw):
        assert raw_figure(text) is raw

    async def test_summary_prompt_passes_formatted_values(self):
        muse = SmartMuse()
        sim = demo(live_llm(muse), scenario="cross_longs")
        captured = []
        original = muse.__call__

        async def spy(request):
            captured.append(json.loads(request.content)["messages"][1]["content"])
            return await original(request)

        muse.__call__ = spy
        llm = MetaLLMClient(api_key="sk-test", use_mock=False,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(spy)))
        await llm.summarize_portfolio(sim.overview, EN, sim.insights().focus)
        facts = json.loads(captured[0])
        assert facts["riskiest_distance"].endswith("% from liquidation")
        assert facts["total_unrealized_pnl"].startswith(("-$", "+$", "$"))
        assert "riskiest_distance_pct" not in facts and not raw_figure(captured[0].replace('"', " "))

    async def test_raw_decimal_headline_is_replaced_by_the_rule_text(self):
        sim = demo(None)
        focus = sim.insights().focus
        symbol = focus["symbol"]

        async def reply(request):
            return httpx.Response(200, json=completion(json.dumps({"headline": f"{symbol} riskiest at 5.15 distance, -7674.0 PnL"})))

        llm = MetaLLMClient(api_key="sk-test", use_mock=False, http_client=httpx.AsyncClient(transport=httpx.MockTransport(reply)))
        result = await llm.summarize_portfolio(sim.overview, EN, focus)
        assert result["mock"] is True and result["source"] == RULES and "5.15" not in result["headline"]
        assert llm.mode == "live"


class TestValidator:
    def test_rounded_and_nearby_amounts_pass(self, plan):
        best = plan.option(plan.recommended)
        cost = best.total_cost_usd or best.capital_required_usd
        base = mock_rescue_explanation(plan)
        shown_as = [f"${cost:,.2f}", f"${cost:,.0f}", f"${round(cost * 1.004, 2):,.2f}"]
        if cost >= 1000:
            shown_as.append(f"${cost / 1000:.2f}k")
        for shown in shown_as:
            answer = {**base, "headline": f"Recommended: {best.kind} for {shown}."}
            assert validate_rescue_explanation(answer, plan) is None, shown

    def test_sum_of_one_options_costs_passes(self, plan):
        o = next(o for o in plan.options if o.fees_usd and o.funding_24h_usd)
        answer = {**mock_rescue_explanation(plan), "headline": f"Fees plus funding: ${o.fees_usd + abs(o.funding_24h_usd):,.2f}."}
        assert validate_rescue_explanation(answer, plan) is None

    def test_invented_amount_is_still_rejected(self, plan):
        answer = {**mock_rescue_explanation(plan), "headline": "Just add $123,456.78."}
        assert "not in plan" in validate_rescue_explanation(answer, plan)


class TestLoad:
    async def test_a_burst_queues_off_the_clock_instead_of_timing_out(self, plan):
        """Five different questions, one slot, 0.3 s each: the budget (0.5 s) is per request, not per wait."""
        muse = SmartMuse(delay=0.3)
        llm = live_llm(muse, timeout=0.5)
        llm.concurrency = 1
        sim = demo(None)
        plans = [sim.build_rescue_plan(s) for s in ("BTCUSDT", "ETH", "SOLUSDT")]
        stops = [sim.protection(p).stop for p in sim.positions if sim.protection(p).stop.needs_warning][:2]
        results = await asyncio.gather(*(llm.explain_rescue(p) for p in plans), *(llm.explain_stop_fix(c) for c in stops))
        assert all(r["mock"] is False and r["source"] == MUSE for r in results)
        assert llm.mode == "live" and muse.calls == len(plans) + len(stops)

    async def test_same_position_asks_once_while_its_numbers_move(self, plan):
        muse = SmartMuse(delay=0.1)
        llm = live_llm(muse)
        first = plan
        second = dataclasses.replace(plan, mark_price=plan.mark_price * 1.001)
        assert first.to_dict() != second.to_dict() and first.recommended == second.recommended
        a, b = await asyncio.gather(llm.explain_rescue(first), llm.explain_rescue(second))
        assert a["mock"] is False and b["mock"] is False and muse.calls == 1

    async def test_warm_caches_before_anyone_opens_it(self, plan):
        muse = SmartMuse()
        service = MuseService(live_llm(muse))
        service.warm(EN, plans=(plan,))
        assert await until(lambda: muse.calls == 1)
        assert await until(lambda: service.rescue(plan, EN).live)
        assert service.rescue(plan, EN).source == MUSE and muse.calls == 1


class TestLabels:
    def test_mock_tag_only_without_a_key(self):
        assert recommendation_line(EN, "Add $500.", MUSE).endswith(t(EN, "alert.muse_mark"))
        assert recommendation_line(EN, "Add $500.", MOCK).endswith(t(EN, "alert.muse_mark_mock"))
        assert recommendation_line(EN, "Add $500.", RULES) == "💡 Add $500."
        assert recommendation_line(ZH, "加 $500。", RULES) == "💡 加 $500。"

    async def test_timeout_keeps_the_rule_text_without_a_mock_tag(self):
        sim = demo(live_llm(SmartMuse(asyncio.Event()), timeout=0.2))
        await sim.start()
        await sim.wait()
        assert sim.advice and sim.snapshot()["muse_mode"] == "fallback"
        from liqguard.alerts.messages import advice_recommendation
        for card in sim.advice:
            rec = advice_recommendation(card)
            assert rec["source"] == RULES
            assert "(mock)" not in recommendation_line(EN, rec["text"], rec["source"])
        stops = [a for a in sim.alerts if a["level"] == "stop"]
        assert stops and all("(mock)" not in format_stop_message(a["stop"], a["explanation"], demo=True) for a in stops)

    async def test_no_key_still_says_mock(self):
        sim = demo(MetaLLMClient(use_mock=True))
        await sim.start()
        await sim.wait()
        stop = next(a for a in sim.alerts if a["level"] == "stop")
        assert t(EN, "alert.muse_mark_mock") in format_stop_message(stop["stop"], stop["explanation"], demo=True)


class TestOneServiceEverywhere:
    def test_simulation_goes_through_muse_service(self):
        llm = MetaLLMClient(use_mock=True)
        sim = demo(llm)
        assert isinstance(sim.muse, MuseService) and sim.llm is llm
        service = MuseService(llm)
        assert demo(service).muse is service

    async def test_app_context_shares_its_service_with_every_simulation(self, ctx):
        assert ctx.simulations.public.muse is ctx.muse
        assert ctx.simulations.get_or_create(42).muse is ctx.muse


class TestPublicDemoOpensWithAnAction:
    def test_public_book_starts_inside_the_warning_tier(self):
        sim = demo(None, open_at_warning=True)
        warning = sim.threshold.warning_distance_pct
        assert sim.step > 0 and any(p.distance_to_liquidation_pct <= warning for p in sim.positions)
        assert all(info.next_funding_time > sim.sim_now for info in sim.funding.values())

    async def test_first_tick_fires_a_real_alert(self):
        sim = demo(MetaLLMClient(use_mock=True), open_at_warning=True, steps=40)
        opening = sim.step
        await sim.start()
        await sim.wait()
        tiers = [a for a in sim.alerts if a["level"] in ("warning", "critical", "emergency", "triage")]
        assert tiers and tiers[0]["step"] == opening + 1

    def test_private_demos_still_start_from_the_beginning(self, ctx):
        assert ctx.simulations.get_or_create(42).step == 0 and ctx.simulations.public.step > 0


class TestTelegramEditsInPlace:
    async def test_demo_stop_alert_is_edited_from_rule_text_to_muse(self):
        gate = asyncio.Event()
        sim = MarketSimulation(live_llm(SmartMuse(gate)), owner_id=7, tick_interval=0, escalation_seconds_per_minute=0.01)
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=lambda *a, **k: MagicMock(message_id=100 + bot.send_message.await_count))
        bot.edit_message_text = AsyncMock()
        TelegramSimulationBridge(bot, sim)
        await sim.start()
        assert await until(lambda: sim.step == sim.steps, timeout=20)
        stop_texts = [c.args[1] for c in bot.send_message.await_args_list if "STOP" in c.args[1]]
        assert stop_texts and all("(mock)" not in text and "✦ Muse" not in text.split("💡", 1)[-1].split("\n")[0]
                                  for text in stop_texts)
        gate.set()
        await sim.wait()
        edits = [c.args[0] for c in bot.edit_message_text.await_args_list]
        assert any("💡 Muse reviewed the stop. · ✦ Muse" in e for e in edits)
        assert any("✦ Muse" in e and "(mock)" not in e for e in edits)
