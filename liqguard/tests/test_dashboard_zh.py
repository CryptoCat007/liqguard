"""The dashboard in zh-Hant: every payload the page renders is Chinese apart from tickers, names and units."""

import pytest
from fastapi.testclient import TestClient

from liqguard.ai import MetaLLMClient
from liqguard.core.position_tools import TOOLS
from liqguard.i18n import EN, ZH, Msg, localize
from liqguard.simulation import SCENARIOS, MarketSimulation
from liqguard.web import create_app

from .zh_scan import english_in, english_words

CSRF = {"X-LiqGuard-CSRF": "1"}
ZH_Q = "lang=zh-Hant"


def demo(scenario=None, language=EN) -> MarketSimulation:
    return MarketSimulation(MetaLLMClient(use_mock=True), tick_interval=0, escalation_seconds_per_minute=0.01,
                            scenario=scenario, language=language)


def collect(c: TestClient, prefix: str, q: str) -> dict:
    snap = c.get(f"{prefix}/snapshot?{q}").json()
    out = {"snapshot": snap}
    for name in ("portfolio", "insights", "alerts", "advice", "audit"):
        out[name] = c.get(f"{prefix}/{name}?{q}").json()
    for symbol in snap["history"]:
        out[f"rescue:{symbol}"] = c.get(f"{prefix}/rescue?symbol={symbol}&{q}").json()
        for tool in TOOLS:
            out[f"{tool}:{symbol}"] = c.get(f"{prefix}/position-tool?symbol={symbol}&tool={tool}&{q}").json()
    return out


def test_scanner_allows_only_tickers_names_and_units():
    assert english_words("BTCUSDT 多單（Binance）距離強平 2.9%，1m 插針 ×1.5，USDT、APR、ADL") == []
    assert english_words("Muse 建議：Telegram 與 LiqGuard 同步，約 3.5 h") == []
    assert english_words("建議「Add $3,729.29 to cross wallet」") == ["Add", "to", "cross", "wallet"]
    assert english_words("Demo 資料") == ["Demo"]


def test_localize_renders_nested_messages():
    data = {"a": [Msg("risk.funding.at_current_rate"), {"b": (Msg("common.side.long"),)}], "n": 1}
    assert localize(ZH, data) == {"a": ["按當前費率", {"b": ["多"]}], "n": 1}
    assert localize(EN, data)["a"][0] == "at current rate"


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_zh_demo_run_has_no_english_in_any_payload(scenario):
    with TestClient(create_app(demo_simulation=demo(scenario))) as c:
        idle = collect(c, "/api/demo", ZH_Q)
        assert c.post(f"/api/demo/simulate?{ZH_Q}", headers=CSRF).json()["status"] == "started"
        zh = next(s for s in _public_sims(c) if s.language == ZH)
        c.portal.call(zh.wait)
        payloads = collect(c, "/api/demo", ZH_Q)
        assert payloads["snapshot"]["state"] == "finished"
        levels = {a["level"] for a in payloads["alerts"]}
        assert {"warning", "critical", "emergency", "sizing", "correlation"} <= levels
        assert payloads["advice"] and payloads["audit"] and payloads["snapshot"]["escalation_log"]
        leftovers = [(name, path, text) for body in (idle, payloads) for name, part in body.items()
                     for path, text in english_in(part)]
        assert not leftovers, leftovers[:10]


def test_each_language_gets_its_own_public_demo():
    sim = demo()
    with TestClient(create_app(demo_simulation=sim)) as c:
        en = c.get("/api/demo/snapshot").json()
        zh = c.get(f"/api/demo/snapshot?{ZH_Q}").json()
        assert en["summary"]["headline"].startswith("Riskiest")
        assert zh["summary"]["headline"].startswith("最高風險")
        assert not english_words(zh["summary"]["headline"])
        c.post(f"/api/demo/simulate?{ZH_Q}", headers=CSRF)
        assert c.get("/api/demo/snapshot").json()["state"] == "idle"
        assert c.get("/api/health").json()["state"] == "idle"


def test_zh_websocket_events_are_chinese():
    sim = demo("cross_longs")
    with TestClient(create_app(demo_simulation=sim)) as c:
        with c.websocket_connect(f"/ws/demo?{ZH_Q}") as ws:
            first = ws.receive_json()
            assert first["type"] == "snapshot" and not list(english_in(first["data"]))
            c.post(f"/api/demo/simulate?{ZH_Q}", headers=CSRF)
            seen, leftovers = set(), []
            while not {"tick", "alert", "advice", "audit"} <= seen:
                event = ws.receive_json()
                seen.add(event["type"])
                leftovers += english_in(event)
            assert not leftovers, leftovers[:10]


def _public_sims(c: TestClient) -> list[MarketSimulation]:
    hubs = c.app.state.hubs._hubs.values()
    return [h.simulation for h in hubs if h.simulation.owner_id is None]

