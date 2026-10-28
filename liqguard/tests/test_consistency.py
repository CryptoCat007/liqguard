"""Every dashboard panel, the summary, Muse and the alerts agree within one snapshot."""

import asyncio
import math
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from liqguard.ai import MetaLLMClient
from liqguard.alerts import format_triage_message
from liqguard.risk.triage import TriageItem, focus
from liqguard.simulation import MarketSimulation

STATIC = Path(__file__).resolve().parents[1] / "web" / "static"


async def run(scenario: str) -> tuple[MarketSimulation, list[dict]]:
    sim = MarketSimulation(MetaLLMClient(use_mock=True), scenario=scenario, tick_interval=0, escalation_seconds_per_minute=0.01)
    events: list[dict] = []

    async def collect(e):
        events.append(e)

    sim.subscribe("test", collect)
    await sim.start()
    await sim.wait()
    await sim.stop()
    return sim, events


def item(symbol: str, distance: float, sigma: float, rank: int, of: int, severity="critical", label=None) -> TriageItem:
    return TriageItem(symbol=symbol, venue="binance", side="long", distance_pct=distance, severity=severity,
                      warning_tier_pct=20, sigma_5m_pct=sigma, sigma_source="wicks",
                      minutes_to_liq=5 * (distance / sigma) ** 2, loss_at_liq_usd=1000, notional=10000,
                      score=50, rank=rank, of=of, label=label)


def assert_tick_consistent(tick: dict) -> None:
    ins, summary, ov = tick["insights"], tick["summary"], tick["overview"]
    triage, foc = ins["triage"], ins["focus"]
    overview_dist = {p["symbol"]: p["distance_to_liquidation_pct"] for p in ov["positions"]}
    step = tick["step"]

    # one distance per position in every panel
    for t in triage:
        sym = t["symbol"]
        assert t["distance_pct"] == pytest.approx(overview_dist[sym], abs=0.01), (step, sym, "triage vs overview")
        assert tick["prices"][sym]["distance_pct"] == pytest.approx(overview_dist[sym], abs=0.01), (step, sym, "chart")
        assert tick["protection"][sym]["distance_pct"] == pytest.approx(overview_dist[sym], abs=0.01), (step, sym, "protection")
    # one riskiest position: triage #1 = focus = summary card = Muse headline
    assert summary["step"] == step
    assert foc["symbol"] == triage[0]["symbol"] == summary["riskiest_symbol"], step
    assert summary["riskiest_distance_pct"] == pytest.approx(overview_dist[foc["symbol"]], abs=0.01)
    assert foc["distance_pct"] == pytest.approx(overview_dist[foc["symbol"]], abs=0.01)
    assert foc["symbol"] in summary["headline"]
    assert f"{overview_dist[foc['symbol']]:.1f}%" in summary["headline"]
    assert summary["min_distance_pct"] == pytest.approx(min(overview_dist.values()), abs=0.01)
    assert ins["top"] is None or ins["top"]["symbol"] == foc["symbol"]
    # a rank that contradicts raw distance is explained in the UI, with the same numbers
    nearest = min(overview_dist, key=overview_dist.get)
    assert foc["nearest_symbol"] == nearest
    if foc["symbol"] != nearest:
        assert foc["basis"] != "closest to liquidation"
        assert nearest in foc["why"] and foc["symbol"] in foc["why"]
        assert f"{overview_dist[nearest]:.1f}%" in foc["why"]
        assert summary["priority_reason"] == foc["why"] and summary["priority_basis"] in summary["headline"]
        if foc["basis"] == "closest in volatility-adjusted terms":
            assert foc["typical_moves"] == min(t["typical_moves"] for t in triage)
    # one account-level liquidating move
    worst = ins["worst_account"]
    if worst is not None:
        acct = next(s for s in ins["stress"] if s["venue"] == worst["venue"])
        assert abs(worst["first_liquidation_move_pct"]) == pytest.approx(
            min(m for m in (acct["liq_move_down_pct"], acct["liq_move_up_pct"]) if m is not None), abs=0.01)


class TestSingleSnapshot:
    @pytest.mark.parametrize("scenario", ["cross_longs", "mixed"])
    async def test_every_tick_is_consistent_across_panels(self, scenario):
        _, events = await run(scenario)
        ticks = [e["data"] for e in events if e["type"] == "tick"]
        assert len(ticks) >= 40
        for tick in ticks:
            assert_tick_consistent(tick)

    async def test_cross_longs_explains_why_volatility_outranks_raw_distance(self):
        _, events = await run("cross_longs")
        late = [e["data"] for e in events if e["type"] == "tick"][-6:]
        for tick in late:
            foc = tick["insights"]["focus"]
            assert foc["nearest_symbol"] == "BTCUSDT" and foc["symbol"] != "BTCUSDT"
            assert foc["basis"] == "closest in volatility-adjusted terms"
            assert "nearer in %" in foc["why"] and "calmer" in foc["why"]
            assert "volatility-adjusted" in tick["summary"]["headline"]

    async def test_alerts_name_the_focus_of_their_own_tick(self):
        _, events = await run("cross_longs")
        latest_tick = None
        for e in events:
            if e["type"] == "tick":
                latest_tick = e["data"]
            elif e["type"] == "alert" and e["data"]["level"] == "triage":
                a, foc = e["data"], latest_tick["insights"]["focus"]
                assert a["step"] == latest_tick["step"]
                assert a["symbol"] == a["items"][0]["symbol"] == a["handle_first"]["symbol"] == foc["symbol"]
                assert a["focus"] == foc
                assert [t["symbol"] for t in a["items"]] == [t["symbol"] for t in latest_tick["insights"]["triage"] if t["at_risk"]]
            elif e["type"] == "alert" and e["data"]["level"] == "correlation" and e["data"].get("stress") and latest_tick:
                a, worst = e["data"], latest_tick["insights"]["worst_account"]
                assert a["stress"]["liq_move_down_pct"] == pytest.approx(abs(worst["first_liquidation_move_pct"]), abs=0.01)

    async def test_snapshot_after_the_run_matches_the_last_tick(self):
        sim, events = await run("cross_longs")
        last = [e["data"] for e in events if e["type"] == "tick"][-1]
        snap = sim.snapshot()
        assert snap["summary"]["riskiest_symbol"] == snap["insights"]["focus"]["symbol"] == last["insights"]["focus"]["symbol"]
        assert snap["insights"]["focus"] == last["insights"]["focus"]
        assert snap["insights"]["worst_account"] == last["insights"]["worst_account"]


class TestStaleSummary:
    async def test_late_muse_headline_from_an_older_step_is_dropped(self):
        llm = MetaLLMClient(api_key="test", use_mock=False)
        sim = MarketSimulation(llm, scenario="cross_longs", tick_interval=0, escalation_seconds_per_minute=0.01)
        sim.step = 30
        sim.positions = sim._positions_at(30)
        sim.overview = sim._analyze()
        sim.summary = sim._tick_summary(sim.insights())
        gate = asyncio.Event()

        async def slow(overview, language, focus):
            await gate.wait()
            return {"headline": "stale", "riskiest_symbol": "BTCUSDT", "mock": False}

        llm.summarize_portfolio = slow
        emitted = []

        async def collect(e):
            emitted.append(e)

        sim.subscribe("test", collect)
        task = asyncio.create_task(sim.refresh_summary())
        await asyncio.sleep(0)
        sim.step = 31
        sim.positions = sim._positions_at(31)
        sim.overview = sim._analyze()
        sim.summary = sim._tick_summary(sim.insights())
        gate.set()
        await task
        assert not any(e["type"] == "summary" for e in emitted)
        assert sim.summary["step"] == 31 and sim.summary["headline"] != "stale"

    async def test_live_headline_quoting_another_number_falls_back(self):
        sim = MarketSimulation(MetaLLMClient(use_mock=True), scenario="cross_longs")
        sim.step = 35
        sim.positions = sim._positions_at(35)
        sim.overview = sim._analyze()
        foc = sim.insights().focus
        client = MetaLLMClient(api_key="test", use_mock=False)
        reply = MagicMock()
        reply.choices = [MagicMock(message=MagicMock(content=f'{{"headline": "{foc["symbol"]} is 9.0% from liquidation"}}'))]
        client.client = MagicMock()
        client.client.chat.completions.create = AsyncMock(return_value=reply)
        result = await client.summarize_portfolio(sim.overview, "en", foc)
        assert result["riskiest_symbol"] == foc["symbol"]
        assert "9.0%" not in result["headline"] and f"{foc['distance_pct']:.1f}%" in result["headline"]


class TestFocus:
    def test_volatile_coin_outranking_the_nearest_is_explained(self):
        items = [item("SOLUSDT", 7.8, 0.52, 1, 2, label="Handle this first"), item("BTCUSDT", 6.9, 0.26, 2, 2)]
        f = focus(items)
        assert f["symbol"] == "SOLUSDT" and f["nearest_symbol"] == "BTCUSDT"
        assert f["basis"] == "closest in volatility-adjusted terms"
        assert "SOLUSDT" in f["why"] and "BTCUSDT is nearer in % (6.9%)" in f["why"] and "15.0 typical 5m moves" in f["why"]

    def test_nearest_on_top_needs_no_excuse(self):
        f = focus([item("BTCUSDT", 5.0, 0.2, 1, 2), item("ETH", 8.0, 0.2, 2, 2)])
        assert f["basis"] == "closest to liquidation" and f["nearest_symbol"] == "BTCUSDT"

    def test_other_reasons_do_not_claim_volatility(self):
        f = focus([item("ETH", 8.0, 0.2, 1, 2), item("BTCUSDT", 5.0, 0.2, 2, 2, severity=None)])
        assert f["basis"] == "inside an alert tier"
        f = focus([item("ETH", 8.0, 0.2, 1, 2), item("BTCUSDT", 5.0, 0.2, 2, 2)])
        assert f["basis"] == "highest combined urgency" and "volatility-adjusted" not in f["why"]

    def test_empty_and_unliquidatable(self):
        assert focus([]) is None
        f = focus([item("BTCUSDT", math.inf, 0.2, 1, 1)])
        assert f["distance_pct"] is None and f["basis"] == "only position"

    def test_triage_message_carries_the_same_reason(self):
        items = [item("SOLUSDT", 7.8, 0.52, 1, 2, label="Handle this first"), item("BTCUSDT", 6.9, 0.26, 2, 2)]
        f = focus(items)
        text = format_triage_message([i.to_dict() for i in items], "critical", focus=f)
        assert f"Why SOLUSDT first: {f['why']}." in text


class TestDashboardWiring:
    """The browser renders from the tick payload and never shows two toasts of one kind."""

    def test_tick_drives_summary_and_toasts_are_keyed(self):
        js = (STATIC / "app.js").read_text()
        assert "if (data.summary) S.summary = data.summary;" in js
        assert "data.step !== S.step" in js
        for key in ('"triage")', '"stress")', '"sizing")', "`tier:${a.symbol}`)"):
            assert key in js
        assert 'querySelectorAll(`.toast[data-key="${key}"]`)' in js
        assert "refreshLiveToasts();" in js
        assert js.count("accountLiqMove()") >= 3

    def test_page_explains_the_ranking(self):
        html = (STATIC / "index.html").read_text()
        assert 'id="triageWhy"' in html and 'id="riskiestWhy"' in html
        assert "Closest to liquidation (raw %)" in html
