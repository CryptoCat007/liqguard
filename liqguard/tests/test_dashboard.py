"""Tests for the public (demo) side of the read-only web dashboard."""

import pytest
from fastapi.testclient import TestClient

from liqguard.ai import MetaLLMClient
from liqguard.simulation import MarketSimulation
from liqguard.web import create_app

CSRF = {"X-LiqGuard-CSRF": "1"}

SECRET = "meta-key-that-must-never-leak"


@pytest.fixture
def sim():
    return MarketSimulation(MetaLLMClient(api_key=SECRET, use_mock=True), tick_interval=0)


@pytest.fixture
def client(sim):
    with TestClient(create_app(demo_simulation=sim)) as c:
        yield c


def run_to_end(client: TestClient, sim: MarketSimulation) -> None:
    assert client.post("/api/demo/simulate", headers=CSRF).json()["status"] == "started"
    client.portal.call(sim.wait)


class TestPages:
    def test_index_served_with_security_headers(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "LiqGuard" in r.text
        assert "Simulate market" in r.text
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"

    @pytest.mark.parametrize("path", ["/static/app.js", "/static/app.css"])
    def test_static_assets(self, client, path):
        assert client.get(path).status_code == 200

    def test_health(self, client):
        body = client.get("/api/health").json()
        assert body == {"status": "ok", "demo": True, "muse_mode": "mock", "state": "idle", "login_enabled": False}


class TestReadEndpoints:
    def test_snapshot_initial_state(self, client):
        snap = client.get("/api/demo/snapshot").json()
        assert snap["state"] == "idle"
        assert snap["demo"] is True
        assert snap["thresholds"] == {"warning": 20.0, "critical": 10.0, "emergency": 5.0}
        assert set(snap["history"]) == {"BTCUSDT", "ETH", "SOLUSDT", "TSLAUSDT"}
        assert snap["summary"]["riskiest_symbol"] in snap["history"]
        assert snap["channels"] == ["web"]
        assert snap["scope"] == "public"

    def test_portfolio_aggregates_both_venues(self, client):
        body = client.get("/api/demo/portfolio").json()
        ov = body["overview"]
        assert ov["venues"] == ["binance", "hyperliquid"]
        assert ov["summary"]["total_positions"] == 4
        notional = sum(p["notional_value"] for p in ov["positions"])
        assert ov["summary"]["total_notional_value"] == pytest.approx(notional, abs=0.05)
        assert body["summary"]["headline"]

    def test_full_run_populates_timeline_advice_audit(self, client, sim):
        run_to_end(client, sim)

        alerts = client.get("/api/demo/alerts").json()
        advice = client.get("/api/demo/advice").json()
        audit = client.get("/api/demo/audit").json()

        assert {a["level"] for a in alerts} == {"warning", "critical", "emergency", "funding", "stop", "adl", "sizing", "correlation"}
        assert len(advice) == len([a for a in alerts if a["level"] in ("warning", "critical", "emergency")])
        events = [e["event"] for e in audit]
        assert events[0] == "simulation_started"
        assert "advice_generated" in events
        assert events[-1] in ("simulation_finished", "advice_generated")

    def test_no_secret_in_any_response(self, client, sim):
        run_to_end(client, sim)
        for path in ["/", "/static/app.js", "/api/health", "/api/demo/snapshot", "/api/demo/portfolio",
                     "/api/demo/alerts", "/api/demo/advice", "/api/demo/audit"]:
            assert SECRET not in client.get(path).text, path


class TestControls:
    def test_simulate_twice_reports_running(self, client):
        slow = MarketSimulation(None, tick_interval=0.2)
        with TestClient(create_app(demo_simulation=slow)) as c:
            assert c.post("/api/demo/simulate", headers=CSRF).json()["status"] == "started"
            assert c.post("/api/demo/simulate", headers=CSRF).json()["status"] == "already_running"
            assert c.post("/api/demo/simulate/stop", headers=CSRF).json()["status"] == "stopped"
            assert c.get("/api/health").json()["state"] == "stopped"

    def test_reset_clears_state(self, client, sim):
        run_to_end(client, sim)
        assert client.post("/api/demo/simulate/reset", headers=CSRF).json()["status"] == "reset"
        assert client.get("/api/demo/alerts").json() == []
        assert client.get("/api/health").json()["state"] == "idle"

    def test_no_execution_endpoints(self, client):
        paths = {r.path for r in client.app.routes}
        for forbidden in ("/api/actions", "/api/execute", "/api/confirm", "/api/bind"):
            assert forbidden not in paths
        posts = {r.path for r in client.app.routes if "POST" in getattr(r, "methods", set())}
        assert posts == {"/api/demo/simulate", "/api/demo/simulate/stop", "/api/demo/simulate/reset", "/api/demo/ack",
                         "/api/demo/scenario", "/auth/logout"}

    def test_post_without_csrf_header_is_rejected(self, client):
        assert client.post("/api/demo/simulate").status_code == 403
        r = client.post("/api/demo/simulate", headers={**CSRF, "Origin": "https://evil.example"})
        assert r.status_code == 403

    def test_public_page_is_labelled_demo(self, client):
        html = client.get("/").text
        assert "DEMO DATA" in html


class TestWebSocket:
    def test_snapshot_then_live_events(self, client, sim):
        with client.websocket_connect("/ws/demo") as ws:
            first = ws.receive_json()
            assert first["type"] == "snapshot"
            assert first["data"]["state"] == "idle"
            assert first["data"]["scope"] == "public"

            client.post("/api/demo/simulate", headers=CSRF)
            seen = set()
            while not {"tick", "alert", "advice", "audit"} <= seen:
                seen.add(ws.receive_json()["type"])

    def test_ping(self, client):
        with client.websocket_connect("/ws/demo") as ws:
            ws.receive_json()
            ws.send_text("ping")
            assert ws.receive_json() == {"type": "pong"}
