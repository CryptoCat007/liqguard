"""Every Muse surface leads with one 💡 recommendation line and a small ✦ Muse tag, never a second block."""

import pytest

from liqguard.ai import MetaLLMClient, mock_stop_explanation
from liqguard.alerts import format_stop_message, recommendation_line
from liqguard.alerts.messages import advice_recommendation
from liqguard.i18n import EN, ZH
from liqguard.risk import check_stop_loss
from liqguard.simulation import MarketSimulation
from liqguard.telegram.sim_bridge import format_advice_message

from .test_protection import pos, stop

LANGS = (EN, ZH)


def muse_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if "✦ Muse" in line]


@pytest.mark.parametrize("lang", LANGS)
def test_recommendation_line_tags_muse_and_mock(lang):
    live = recommendation_line(lang, "Add $500.", True)
    mock = recommendation_line(lang, "Add $500.", False)
    assert live == "💡 Add $500. · ✦ Muse"
    assert mock.startswith("💡 Add $500. · ✦ Muse") and mock != live


@pytest.mark.parametrize("lang", LANGS)
async def test_sim_advice_has_one_line_at_the_top(lang):
    sim = MarketSimulation(MetaLLMClient(use_mock=True), tick_interval=0, escalation_seconds_per_minute=0.01, language=lang)
    await sim.start()
    await sim.wait()
    assert sim.advice
    for card in sim.advice:
        text = format_advice_message(card, lang)
        lines = text.splitlines()
        rec = muse_lines(text)
        assert len(rec) == 1 and rec[0].startswith("💡 ")
        assert lines.index(rec[0]) <= 5
        assert "🧠" not in text


def test_live_muse_wording_replaces_the_rule_text():
    card = {"margin_to_add_usd": 100.0, "margin_mode": "cross", "target_distance_pct": 30.0, "reduce_size_pct": 5.0,
            "explanation": "rule text", "mock": True}
    assert advice_recommendation(card)["live"] is False
    live = advice_recommendation({**card, "explanation": "Muse says add $100.", "mock": False})
    assert live == {"text": "Muse says add $100.", "live": True, "source": "muse"}


@pytest.mark.parametrize("lang", LANGS)
def test_stop_message_has_one_muse_line(lang):
    check = check_stop_loss(pos(), [stop(89.0)], 90.0)
    text = format_stop_message(check.to_dict(), mock_stop_explanation(check, lang), lang=lang)
    rec = muse_lines(text)
    assert len(rec) == 1 and rec[0].startswith("💡 ")
    assert "🧠" not in text
