"""Dashboard surface for wick stress, stop-loss checks, ADL and the "I'm awake" acknowledgement."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from liqguard.ai import MetaLLMClient
from liqguard.models import User
from liqguard.simulation import MarketSimulation
from liqguard.web import create_app
from liqguard.web.auth import SESSION_COOKIE

CSRF = {"X-LiqGuard-CSRF": "1"}
ALICE, BOB = 1001, 2002


@pytest.fixture
def sim():
    return MarketSimulation(MetaLLMClient(use_mock=True), tick_interval=0, escalation_seconds_per_minute=0.01)


@pytest.fixture
def client(sim):
    with TestClient(create_app(demo_simulation=sim)) as c:
        yield c
        c.portal.call(sim.stop)


def run_to_end(client: TestClient, sim: MarketSimulation) -> None:
    assert client.post("/api/demo/simulate", headers=CSRF).json()["status"] == "started"
    client.portal.call(sim.wait)


class TestDemoSnapshot:
    def test_snapshot_has_protection_for_every_position(self, client):
        snap = client.get("/api/demo/snapshot").json()
        assert set(snap["protection"]) == set(snap["history"])
        btc = snap["protection"]["BTCUSDT"]
        assert btc["stop"]["status"] == "beyond_liquidation"
        assert btc["wicks"]["exposures"][0]["interval"] == "1m"
        assert btc["thresholds"]["effective"]["warning"] > btc["thresholds"]["base"]["warning"]
        assert snap["protection"]["ETH"]["adl"]["supported"] is False
        assert snap["protection"]["ETH"]["stop"]["status"] == "no_stop"
        assert snap["session"]["weekend"] is True
        assert snap["escalation"]["enabled"] is True and snap["escalation"]["pending"] == []

    def test_run_produces_stop_adl_and_acknowledgeable_tier_alerts(self, client, sim):
        run_to_end(client, sim)
        alerts = client.get("/api/demo/alerts").json()
        levels = {a["level"] for a in alerts}
        assert {"stop", "adl", "critical", "emergency"} <= levels
        stop = next(a for a in alerts if a["level"] == "stop")
        assert stop["explanation"]["suggested_fix"] in {f["kind"] for f in stop["stop"]["fixes"]}
        assert any(a.get("ack_id") for a in alerts if a["level"] == "critical")


class TestDemoAck:
    def test_ack_stops_reminders_once(self, client, sim):
        run_to_end(client, sim)
        ack_id = next(a["ack_id"] for a in sim.alerts if a.get("ack_id"))
        assert client.post("/api/demo/ack", json={"ack_id": ack_id}).status_code == 403
        r = client.post("/api/demo/ack", json={"ack_id": ack_id}, headers=CSRF)
        assert r.status_code == 200 and r.json()["status"] == "acknowledged"
        assert client.post("/api/demo/ack", json={"ack_id": ack_id}, headers=CSRF).status_code == 404
        assert any(e["event"] == "alert_acknowledged" for e in client.get("/api/demo/audit").json())

    @pytest.mark.parametrize("bad", ["", "x" * 65, "../etc", "a b"])
    def test_ack_rejects_malformed_ids(self, client, bad):
        assert client.post("/api/demo/ack", json={"ack_id": bad}, headers=CSRF).status_code == 422

    def test_reminders_escalate_and_reach_the_snapshot(self, client, sim):
        run_to_end(client, sim)
        for _ in range(200):
            if sim.escalation_log:
                break
            client.portal.call(asyncio.sleep, 0.02)
        log = client.get("/api/demo/snapshot").json()["escalation_log"]
        assert log and log[0]["reminder"] == 1 and "I'm awake" in log[0]["text"]


@pytest.fixture
def dashboard(ctx):
    with TestClient(create_app(ctx), base_url="https://testserver") as client:
        yield client


def sign_in(client: TestClient, ctx, user_id: int) -> None:
    client.cookies.set(SESSION_COOKIE, ctx.sessions.issue(user_id, 0))


class TestPrivateDashboard:
    async def test_account_includes_protection_and_health_lists_wick_feeds(self, ctx, dashboard):
        await ctx.storage.save_user(User(telegram_id=ALICE, is_demo_mode=True))
        sign_in(dashboard, ctx, ALICE)
        body = dashboard.get("/api/me/account").json()
        stops = {p["symbol"]: p["stop"]["status"] for p in body["protection"]}
        assert stops["BTCUSDT"] == "beyond_liquidation"
        assert any(p["adl"].get("lights") for p in body["protection"])
        assert body["escalations"] == []
        assert "wick_feeds" in dashboard.get("/api/health").json()

    async def test_live_ack_is_scoped_to_the_session_user(self, ctx, dashboard):
        for uid in (ALICE, BOB):
            await ctx.storage.save_user(User(telegram_id=uid))
        ack = ctx.escalations.open(ALICE, "binance", "BTCUSDT", "critical", "🚨", time.time(), 120, 5)

        sign_in(dashboard, ctx, BOB)
        assert dashboard.get("/api/me/account").json()["escalations"] == []
        assert dashboard.post("/api/me/live/ack", json={"ack_id": ack.id}, headers=CSRF).status_code == 404
        assert ack.acknowledged_at is None

        sign_in(dashboard, ctx, ALICE)
        assert [e["id"] for e in dashboard.get("/api/me/account").json()["escalations"]] == [ack.id]
        assert dashboard.post("/api/me/live/ack", json={"ack_id": ack.id}, headers=CSRF).status_code == 200
        assert ack.acknowledged_at is not None

    async def test_sim_ack_cannot_cross_users(self, ctx, dashboard):
        for uid in (ALICE, BOB):
            await ctx.storage.save_user(User(telegram_id=uid))
        sign_in(dashboard, ctx, ALICE)
        assert dashboard.post("/api/me/simulate", headers=CSRF).status_code == 200
        sim = ctx.simulations.get(ALICE)
        for _ in range(400):
            if sim.state == "finished":
                break
            await asyncio.sleep(0.05)
        ack_id = next(a["ack_id"] for a in sim.alerts if a.get("ack_id"))

        sign_in(dashboard, ctx, BOB)
        assert dashboard.post("/api/me/ack", json={"ack_id": ack_id}, headers=CSRF).status_code == 404
        sign_in(dashboard, ctx, ALICE)
        assert dashboard.post("/api/me/ack", json={"ack_id": ack_id}, headers=CSRF).status_code == 200
        dashboard.portal.call(sim.stop)

    def test_ack_requires_a_session(self, dashboard):
        assert dashboard.post("/api/me/ack", json={"ack_id": "abc"}, headers=CSRF).status_code == 401
        assert dashboard.post("/api/me/live/ack", json={"ack_id": "abc"}, headers=CSRF).status_code == 401
