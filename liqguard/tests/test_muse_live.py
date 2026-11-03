"""
Opt-in: talk to the real Meta API. Runs only with META_API_KEY set and
LIQGUARD_LIVE_TESTS=1; every other run skips it.

    LIQGUARD_LIVE_TESTS=1 META_API_KEY=... python3 -m pytest -q liqguard/tests/test_muse_live.py

The API must answer every time (Muse's status stays live). Muse's wording is a
sample, though, and the validator turns down a sample that, say, prints a raw
decimal, so each test allows LIVE_SAMPLES samples for one validated answer
(a turned-down answer is not cached, so each attempt asks again).
"""

import os

import pytest

from liqguard.ai import MetaLLMClient
from liqguard.ai.rescue import explainable
from liqguard.models import Position, Side, Venue
from liqguard.risk.portfolio import PortfolioAnalyzer
from liqguard.risk.rescue import RescueSimulator

pytestmark = pytest.mark.skipif(
    not (os.environ.get("META_API_KEY") and os.environ.get("LIQGUARD_LIVE_TESTS") == "1"),
    reason="live Meta API test: set META_API_KEY and LIQGUARD_LIVE_TESTS=1",
)

LIVE_SAMPLES = 3

POSITIONS = [
    Position(Venue.BINANCE, "BTCUSDT", Side.LONG, 0.5, 65000, 60000, 57500, 3000, 10, -2500),
    Position(Venue.HYPERLIQUID, "ETH", Side.SHORT, 5, 3200, 3100, 4000, 4000, 4, 500),
]


@pytest.fixture
def llm():
    client = MetaLLMClient(timeout=float(os.environ.get("META_TIMEOUT") or 30))
    assert not client.is_mock
    return client


async def validated(llm: MetaLLMClient, ask) -> dict:
    turned_down = []
    for _ in range(LIVE_SAMPLES):
        result = await ask()
        assert llm.mode == "live", llm.status()
        if result["mock"] is False:
            return result
        turned_down.append(llm.last_error)
    pytest.fail(f"Muse answered {LIVE_SAMPLES} times but every answer was turned down: {turned_down}")


@pytest.mark.parametrize("lang", ["en", "zh-Hant"])
async def test_portfolio_headline(llm, lang):
    overview = PortfolioAnalyzer().analyze_portfolio(POSITIONS)
    result = await validated(llm, lambda: llm.summarize_portfolio(overview, lang))
    assert "BTCUSDT" in result["headline"]


async def test_rescue_explanation(llm):
    plan = RescueSimulator().simulate(POSITIONS[0])
    result = await validated(llm, lambda: llm.explain_rescue(plan, "en"))
    assert result["recommended_option"] in {o.kind for o in explainable(plan)}
