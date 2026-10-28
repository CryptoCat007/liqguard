"""
End-to-end, two real-looking users, with only the exchange HTTP APIs faked:

Telegram onboarding (/start, /bind) → key-permission check → encrypted storage →
monitor alerts → /dashboard login link → private dashboard → /unbind.

Alice connects a read-only Binance key; Bob connects a Hyperliquid wallet (and
first tries a Binance key with trading enabled, which must be rejected).
Neither may ever see, receive or decrypt the other's data.
"""

import functools
import json
from pathlib import Path

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi.testclient import TestClient

from liqguard.alerts import PositionMonitor
from liqguard.web import create_app

from .conftest import ALICE, BOB, fake_callback, fake_message

ALICE_KEY, ALICE_SECRET = "A1" * 32, "a9" * 32
BOB_TRADING_KEY, BOB_TRADING_SECRET = "B7" * 32, "b3" * 32
BOB_WALLET = "0x" + "b0b5" * 10


def fake_exchanges(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.hyperliquid.xyz":
        body = json.loads(request.content)
        if body["type"] == "clearinghouseState":
            if body["user"] != BOB_WALLET:
                return httpx.Response(200, json={"assetPositions": []})
            return httpx.Response(200, json={"assetPositions": [{"position": {
                "coin": "ETH", "szi": "-2", "entryPx": "3000", "liquidationPx": "3150",
                "marginUsed": "600", "leverage": {"value": 10}, "unrealizedPnl": "-120",
            }}]})
        if body["type"] == "meta":
            return httpx.Response(200, json={"universe": [{"name": "ETH"}]})
        if body["type"] == "allMids":
            return httpx.Response(200, json={"ETH": "3060"})

    key = request.headers.get("X-MBX-APIKEY")
    if request.url.path == "/sapi/v1/account/apiRestrictions":
        restrictions = {"enableReading": True, "ipRestrict": True}
        if key == BOB_TRADING_KEY:
            restrictions["enableFutures"] = True
        return httpx.Response(200, json=restrictions)
    if request.url.path == "/fapi/v2/positionRisk":
        assert key == ALICE_KEY, "only Alice's key may ever be used for Alice's positions"
        return httpx.Response(200, json=[{
            "symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "64000", "markPrice": "60000",
            "liquidationPrice": "57500", "leverage": "10", "unRealizedProfit": "-2000",
            "marginType": "cross", "isolatedMargin": "0",
        }])
    if request.url.path == "/fapi/v2/account":
        assert key == ALICE_KEY
        return httpx.Response(200, json={"assets": [{"asset": "USDT", "crossWalletBalance": "5000"}]})
    if request.url.path == "/fapi/v1/leverageBracket":
        assert key == ALICE_KEY
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "brackets": [
            {"bracket": 1, "initialLeverage": 125, "notionalCap": 50000, "notionalFloor": 0, "maintMarginRatio": 0.004, "cum": 0},
            {"bracket": 2, "initialLeverage": 100, "notionalCap": 250000, "notionalFloor": 50000, "maintMarginRatio": 0.005, "cum": 50},
        ]}])
    return httpx.Response(404, json={"msg": f"unexpected {request.method} {request.url}"})


@pytest.fixture(autouse=True)
def mocked_exchanges(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", functools.partial(real, transport=httpx.MockTransport(fake_exchanges)))


@pytest.fixture
def fsm():
    storage = MemoryStorage()
    return lambda uid: FSMContext(storage, StorageKey(bot_id=1, chat_id=uid, user_id=uid))


@pytest.fixture
def handlers(ctx):
    from liqguard.telegram import BotHandlers

    return BotHandlers(ctx)


def last_text(message) -> str:
    status = message.answer.return_value
    if status.edit_text.await_count:
        return status.edit_text.call_args.args[0]
    return message.answer.call_args.args[0]


async def bind_binance(handlers, fsm, uid: int, key: str, secret: str) -> str:
    state = fsm(uid)
    await handlers.cmd_bind(fake_message(uid, "/bind"), state)
    await handlers.callback_venue_selected(fake_callback(uid, "venue:binance"), state)
    key_msg = fake_message(uid, key)
    await handlers.process_binance_api_key(key_msg, state)
    key_msg.delete.assert_awaited()
    secret_msg = fake_message(uid, secret)
    await handlers.process_binance_api_secret(secret_msg, state)
    secret_msg.delete.assert_awaited()
    assert await state.get_state() is None
    return last_text(secret_msg)


async def bind_hyperliquid(handlers, fsm, uid: int, wallet: str) -> str:
    state = fsm(uid)
    await handlers.cmd_bind(fake_message(uid, "/bind"), state)
    await handlers.callback_venue_selected(fake_callback(uid, "venue:hyperliquid"), state)
    message = fake_message(uid, wallet)
    await handlers.process_hyperliquid_address(message, state)
    return last_text(message)


async def login_link(handlers, uid: int) -> str:
    message = fake_message(uid, "/dashboard")
    await handlers.cmd_dashboard(message, None)
    text = message.answer.call_args.args[0]
    return "/auth/login?token=" + text.split("token=")[1].split()[0]


def database_bytes(db_path: str) -> bytes:
    return b"".join(p.read_bytes() for p in Path(db_path).parent.glob(Path(db_path).name + "*"))


async def test_two_users_end_to_end(ctx, handlers, fsm):
    # ---- onboarding in Telegram
    for uid in (ALICE, BOB):
        start = fake_message(uid, "/start")
        await handlers.cmd_start(start)
        assert "Welcome to LiqGuard" in start.answer.call_args.args[0]

    assert "Binance connected" in await bind_binance(handlers, fsm, ALICE, ALICE_KEY, ALICE_SECRET)

    rejected = await bind_binance(handlers, fsm, BOB, BOB_TRADING_KEY, BOB_TRADING_SECRET)
    assert "Rejected" in rejected and "FUTURES_TRADE" in rejected
    assert (await ctx.storage.get_user(BOB)).bindings == []

    assert "Hyperliquid wallet connected" in await bind_hyperliquid(handlers, fsm, BOB, BOB_WALLET)

    # ---- encrypted at rest, each with the owner's own data key
    raw = database_bytes(ctx.settings.db_path)
    for secret in (ALICE_KEY, ALICE_SECRET, BOB_WALLET, BOB_TRADING_KEY):
        assert secret.encode() not in raw
    alice, bob = await ctx.storage.get_user(ALICE), await ctx.storage.get_user(BOB)
    assert [(b.venue, b.api_key) for b in alice.bindings] == [("binance", ALICE_KEY)]
    assert [(b.venue, b.wallet_address) for b in bob.bindings] == [("hyperliquid", BOB_WALLET)]

    # ---- /positions in Telegram shows only your own book
    for uid, mine, theirs in ((ALICE, "BTCUSDT", "ETH"), (BOB, "ETH", "BTCUSDT")):
        message = fake_message(uid, "/positions")
        await handlers.cmd_positions(message)
        text = message.answer.call_args.args[0]
        assert mine in text and theirs not in text

    # ---- the monitor alerts each owner about their own position only
    sent: list[tuple[int, str]] = []

    class Notifier:
        async def notify(self, user_id: int, text: str, actions=None) -> None:
            sent.append((user_id, text))

    monitor = PositionMonitor(
        ctx.storage.get_all_users_with_bindings, ctx.positions.snapshot_for, Notifier(), ctx.alerts, ctx.llm
    )
    await monitor.check_all_users()
    assert {uid for uid, _ in sent} == {ALICE, BOB}
    for uid, text in sent:
        assert ("BTCUSDT" in text) == (uid == ALICE)
        assert ("ETH" in text) == (uid == BOB)

    # ---- private dashboards
    app = create_app(ctx)
    alice_link, bob_link = await login_link(handlers, ALICE), await login_link(handlers, BOB)
    with TestClient(app, base_url="https://testserver") as alice_web, TestClient(app, base_url="https://testserver") as bob_web:
        assert alice_web.get(alice_link, follow_redirects=False).headers["location"] == "/me"
        assert bob_web.get(bob_link, follow_redirects=False).headers["location"] == "/me"
        assert alice_web.get(bob_link, follow_redirects=False).headers["location"] == "/login?error=link"

        alice_account = alice_web.get("/api/me/account").json()
        bob_account = bob_web.get("/api/me/account").json()

        assert [b["venue"] for b in alice_account["bindings"]] == ["binance"]
        assert [p["symbol"] for p in alice_account["positions"]["positions"]] == ["BTCUSDT"]
        assert [b["venue"] for b in bob_account["bindings"]] == ["hyperliquid"]
        assert [p["symbol"] for p in bob_account["positions"]["positions"]] == ["ETH-PERP"]

        alice_body, bob_body = json.dumps(alice_account), json.dumps(bob_account)
        for secret in (ALICE_KEY, ALICE_SECRET, BOB_WALLET):
            assert secret not in alice_body and secret not in bob_body
        assert "ETH" not in alice_body and "hyperliquid" not in alice_body
        assert "BTCUSDT" not in bob_body and "binance" not in bob_body

        # ---- Alice unbinds; Bob is unaffected
        unbind = fake_callback(ALICE, "unbind:binance:mainnet")
        await handlers.callback_unbind_venue(unbind)
        assert "disconnected" in unbind.message.edit_text.call_args.args[0]
        assert alice_web.get("/api/me/account").json()["bindings"] == []
        assert [b["venue"] for b in bob_web.get("/api/me/account").json()["bindings"]] == ["hyperliquid"]

        # ---- Alice signs out everywhere; Bob stays signed in
        revoke = fake_message(ALICE, "/dashboard revoke")
        from unittest.mock import MagicMock

        await handlers.cmd_dashboard(revoke, MagicMock(args="revoke"))
        assert alice_web.get("/api/me/session").status_code == 401
        assert bob_web.get("/api/me/session").json()["user_id"] == BOB


async def test_another_users_bind_flow_state_is_separate(handlers, fsm, ctx):
    """Alice's half-finished /bind (API key sent, secret pending) must not leak into Bob's chat state."""
    await handlers.cmd_start(fake_message(ALICE))
    alice_state = fsm(ALICE)
    await handlers.cmd_bind(fake_message(ALICE, "/bind"), alice_state)
    await handlers.callback_venue_selected(fake_callback(ALICE, "venue:binance"), alice_state)
    await handlers.process_binance_api_key(fake_message(ALICE, ALICE_KEY), alice_state)

    bob_state = fsm(BOB)
    assert await bob_state.get_data() == {}
    message = fake_message(BOB, ALICE_SECRET)
    await handlers.process_binance_api_secret(message, bob_state)
    assert "Session expired" in message.answer.call_args.args[0]
    assert await ctx.storage.get_user(BOB) is None or (await ctx.storage.get_user(BOB)).bindings == []


async def test_dashboard_command_without_a_dashboard(ctx, handlers):
    import dataclasses

    ctx.settings = dataclasses.replace(ctx.settings, public_url=None, dashboard_enabled=False)
    message = fake_message(ALICE, "/dashboard")
    await handlers.cmd_dashboard(message, None)
    assert "not enabled" in message.answer.call_args.args[0]
    assert "token=" not in message.answer.call_args.args[0]


def test_dashboard_url_never_points_at_wildcard_address():
    from liqguard.config import Settings

    assert Settings(dashboard_host="0.0.0.0", dashboard_port=9000).dashboard_url == "http://127.0.0.1:9000"
    assert Settings(public_url="https://guard.example").dashboard_url == "https://guard.example"
