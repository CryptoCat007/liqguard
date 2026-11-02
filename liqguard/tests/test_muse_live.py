"""
Opt-in: talk to the real Meta API. Runs only with META_API_KEY set and
LIQGUARD_LIVE_TESTS=1; every other run skips it.

    LIQGUARD_LIVE_TESTS=1 META_API_KEY=... python3 -m pytest -q liqguard/tests/test_muse_live.py
"""

import os

import pytest

from liqguard.ai import MetaLLMClient
from liqguard.models import Position, Side, Venue
from liqguard.risk.portfolio import PortfolioAnalyzer
from liqguard.risk.rescue import RescueSimulator

pytestmark = pytest.mark.skipif(
    not (os.environ.get("META_API_KEY") and os.environ.get("LIQGUARD_LIVE_TESTS") == "1"),
    reason="live Meta API test: set META_API_KEY and LIQGUARD_LIVE_TESTS=1",
)

POSITIONS = [
    Position(Venue.BINANCE, "BTCUSDT", Side.LONG, 0.5, 65000, 60000, 57500, 3000, 10, -2500),
    Position(Venue.HYPERLIQUID, "ETH", Side.SHORT, 5, 3200, 3100, 4000, 4000, 4, 500),
]


@pytest.fixture
def llm():
    client = MetaLLMClient(timeout=float(os.environ.get("META_TIMEOUT") or 30))
    assert not client.is_mock
    return client


@pytest.mark.parametrize("lang", ["en", "zh-Hant"])
async def test_portfolio_headline(llm, lang):
    result = await llm.summarize_portfolio(PortfolioAnalyzer().analyze_portfolio(POSITIONS), lang)
    assert llm.mode == "live", llm.status()
    assert result["mock"] is False and "BTCUSDT" in result["headline"]


async def test_rescue_explanation(llm):
    plan = RescueSimulator().simulate(POSITIONS[0])
    result = await llm.explain_rescue(plan, "en")
    assert llm.mode == "live", llm.status()
    assert result["mock"] is False and result["recommended_option"] == plan.recommended
